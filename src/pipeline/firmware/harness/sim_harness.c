/*
 * simavr harness — loads a compiled ATmega328P ELF and attaches virtual
 * peripherals described by sim_config.json.  Communicates with the host
 * process via line-delimited JSON on stdin (commands) / stdout (events).
 *
 * Build:  make            (requires libsimavr-dev, libelf-dev, cJSON)
 * Usage:  ./sim_harness <sim_config.json>
 *
 * stdin  commands:
 *   {"cmd":"press","instance_id":"btn_power"}
 *   {"cmd":"release","instance_id":"btn_power"}
 *   {"cmd":"quit"}
 *
 * stdout events:
 *   {"event":"boot_ok"}
 *   {"event":"pin_change","instance_id":"led_top","on":true}
 *   {"event":"error","message":"..."}
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>

#include "sim_avr.h"
#include "avr_ioport.h"
#include "avr_uart.h"
#include "sim_elf.h"
#include <cjson/cJSON.h>

#define MAX_PERIPHERALS 32
#define LINE_BUF 4096

/* ── peripheral descriptor ─────────────────────────────────────── */

typedef enum { PTYPE_BUTTON, PTYPE_LED, PTYPE_IR_OUTPUT } ptype_t;

typedef struct {
    char instance_id[64];
    ptype_t type;
    char port;          /* 'B','C','D' */
    int  pin;           /* 0-7 */
    int  active_low;    /* buttons only */
    int  last_high;     /* last reported pin state (outputs) */
} peripheral_t;

static peripheral_t peripherals[MAX_PERIPHERALS];
static int           n_periph = 0;
static avr_t        *avr      = NULL;
static volatile int  running  = 1;
static pthread_mutex_t stdout_lock = PTHREAD_MUTEX_INITIALIZER;

/* ── serial (UART) capture buffer ──────────────────────────────── */

#define SERIAL_BUF_SZ 1024
static char serial_buf[SERIAL_BUF_SZ];
static int  serial_len = 0;

/* ── helpers ───────────────────────────────────────────────────── */

static void emit_json(const char *json_str) {
    pthread_mutex_lock(&stdout_lock);
    fputs(json_str, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    pthread_mutex_unlock(&stdout_lock);
}

static void emit_event(const char *event, const char *extra) {
    char buf[512];
    if (extra)
        snprintf(buf, sizeof(buf), "{\"event\":\"%s\",%s}", event, extra);
    else
        snprintf(buf, sizeof(buf), "{\"event\":\"%s\"}", event);
    emit_json(buf);
}

static void emit_pin_change(const char *instance_id, int on) {
    char buf[256];
    snprintf(buf, sizeof(buf),
        "{\"event\":\"pin_change\",\"instance_id\":\"%s\",\"on\":%s}",
        instance_id, on ? "true" : "false");
    emit_json(buf);
}

static void emit_error(const char *msg) {
    char buf[512];
    snprintf(buf, sizeof(buf),
        "{\"event\":\"error\",\"message\":\"%s\"}", msg);
    emit_json(buf);
}

/* ── flush accumulated serial data as a JSON event ───────────── */

static void flush_serial(void) {
    if (serial_len == 0) return;
    serial_buf[serial_len] = '\0';

    /* JSON-escape the buffer content */
    char escaped[SERIAL_BUF_SZ * 2];
    int  ei = 0;
    for (int i = 0; i < serial_len && ei < (int)sizeof(escaped) - 6; i++) {
        unsigned char ch = (unsigned char)serial_buf[i];
        switch (ch) {
            case '"':  escaped[ei++]='\\'; escaped[ei++]='"';  break;
            case '\\': escaped[ei++]='\\'; escaped[ei++]='\\'; break;
            case '\n': escaped[ei++]='\\'; escaped[ei++]='n';  break;
            case '\r': escaped[ei++]='\\'; escaped[ei++]='r';  break;
            case '\t': escaped[ei++]='\\'; escaped[ei++]='t';  break;
            default:
                if (ch >= 0x20)
                    escaped[ei++] = ch;
                break;
        }
    }
    escaped[ei] = '\0';
    serial_len = 0;

    char out[SERIAL_BUF_SZ * 2 + 64];
    snprintf(out, sizeof(out),
        "{\"event\":\"serial\",\"data\":\"%s\"}", escaped);
    emit_json(out);
}

/* ── UART output IRQ: called for each byte the firmware sends ── */

static void uart_output_hook(struct avr_irq_t *irq, uint32_t value, void *param) {
    (void)irq; (void)param;
    char ch = (char)(value & 0xFF);

    if (serial_len < SERIAL_BUF_SZ - 1)
        serial_buf[serial_len++] = ch;

    /* Flush on newline or when buffer is nearly full */
    if (ch == '\n' || serial_len >= SERIAL_BUF_SZ - 2)
        flush_serial();
}

/* ── IRQ callback: fired when an output pin changes ──────────── */

static void pin_change_hook(struct avr_irq_t *irq, uint32_t value, void *param) {
    peripheral_t *p = (peripheral_t *)param;
    int high = value ? 1 : 0;
    if (high != p->last_high) {
        p->last_high = high;
        emit_pin_change(p->instance_id, high);
    }
}

/* ── parse sim_config.json ───────────────────────────────────── */

static int load_config(const char *path, char *elf_path_out, size_t elf_sz) {
    FILE *f = fopen(path, "r");
    if (!f) { emit_error("cannot open sim_config.json"); return -1; }

    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc(len + 1);
    if (!buf) { fclose(f); return -1; }
    fread(buf, 1, len, f);
    buf[len] = '\0';
    fclose(f);

    cJSON *root = cJSON_Parse(buf);
    free(buf);
    if (!root) { emit_error("invalid JSON in sim_config"); return -1; }

    cJSON *elf = cJSON_GetObjectItem(root, "elf_path");
    if (cJSON_IsString(elf) && elf->valuestring) {
        /* elf_path in config is relative to the session dir.
           The config path itself is <session_dir>/sim_config.json,
           so derive the session dir from the config path. */
        char config_dir[1024];
        strncpy(config_dir, path, sizeof(config_dir) - 1);
        config_dir[sizeof(config_dir) - 1] = '\0';
        char *slash = strrchr(config_dir, '/');
        if (!slash) slash = strrchr(config_dir, '\\');
        if (slash) *slash = '\0';
        else strcpy(config_dir, ".");
        snprintf(elf_path_out, elf_sz, "%s/%s", config_dir, elf->valuestring);
    } else {
        emit_error("elf_path missing or null"); cJSON_Delete(root); return -1;
    }

    cJSON *perArr = cJSON_GetObjectItem(root, "peripherals");
    if (!cJSON_IsArray(perArr)) { cJSON_Delete(root); return 0; }

    int cnt = cJSON_GetArraySize(perArr);
    for (int i = 0; i < cnt && n_periph < MAX_PERIPHERALS; i++) {
        cJSON *item = cJSON_GetArrayItem(perArr, i);
        peripheral_t *p = &peripherals[n_periph];
        memset(p, 0, sizeof(*p));

        cJSON *iid  = cJSON_GetObjectItem(item, "instance_id");
        cJSON *type = cJSON_GetObjectItem(item, "type");
        cJSON *port = cJSON_GetObjectItem(item, "port");
        cJSON *pin  = cJSON_GetObjectItem(item, "pin");

        if (!cJSON_IsString(iid) || !cJSON_IsString(type) ||
            !cJSON_IsString(port) || !cJSON_IsNumber(pin))
            continue;

        strncpy(p->instance_id, iid->valuestring, sizeof(p->instance_id) - 1);
        p->port = port->valuestring[0];
        p->pin  = pin->valueint;

        if (strcmp(type->valuestring, "button") == 0) {
            p->type = PTYPE_BUTTON;
            cJSON *al = cJSON_GetObjectItem(item, "active_low");
            p->active_low = (cJSON_IsBool(al) && cJSON_IsTrue(al)) ? 1 : 0;
        } else if (strcmp(type->valuestring, "led") == 0) {
            p->type = PTYPE_LED;
        } else if (strcmp(type->valuestring, "ir_output") == 0) {
            p->type = PTYPE_IR_OUTPUT;
        } else {
            continue;
        }
        n_periph++;
    }

    cJSON_Delete(root);
    return 0;
}

/* ── attach peripherals to simavr IRQs ───────────────────────── */

static void attach_peripherals(void) {
    for (int i = 0; i < n_periph; i++) {
        peripheral_t *p = &peripherals[i];
        avr_irq_t *irq;

        if (p->type == PTYPE_BUTTON) {
            /* For input pins we drive the pin value from outside.
               Set initial state: active_low means pulled HIGH at rest. */
            int rest_val = p->active_low ? 1 : 0;
            irq = avr_io_getirq(avr,
                AVR_IOCTL_IOPORT_GETIRQ(p->port), p->pin);
            if (irq)
                avr_raise_irq(irq, rest_val);
        } else {
            /* Output pin — listen for changes via the port output IRQ */
            irq = avr_io_getirq(avr,
                AVR_IOCTL_IOPORT_GETIRQ(p->port), p->pin);
            if (irq)
                avr_irq_register_notify(irq, pin_change_hook, p);
        }
    }
}

/* ── press / release a button ────────────────────────────────── */

static void set_button(const char *instance_id, int pressed) {
    for (int i = 0; i < n_periph; i++) {
        peripheral_t *p = &peripherals[i];
        if (p->type != PTYPE_BUTTON) continue;
        if (strcmp(p->instance_id, instance_id) != 0) continue;

        int val;
        if (p->active_low)
            val = pressed ? 0 : 1;   /* pressed = driven LOW */
        else
            val = pressed ? 1 : 0;

        avr_irq_t *irq = avr_io_getirq(avr,
            AVR_IOCTL_IOPORT_GETIRQ(p->port), p->pin);
        if (irq)
            avr_raise_irq(irq, val);
        return;
    }
}

/* ── AVR run thread ──────────────────────────────────────────── */

static void *avr_run_thread(void *arg) {
    (void)arg;
    while (running) {
        int state = avr_run(avr);
        if (state == cpu_Done || state == cpu_Crashed) {
            if (state == cpu_Crashed)
                emit_error("AVR CPU crashed");
            running = 0;
            break;
        }
    }
    return NULL;
}

/* ── stdin command reader (main thread) ──────────────────────── */

static void process_commands(void) {
    char line[LINE_BUF];
    while (running && fgets(line, sizeof(line), stdin)) {
        cJSON *cmd = cJSON_Parse(line);
        if (!cmd) continue;

        cJSON *c = cJSON_GetObjectItem(cmd, "cmd");
        if (!cJSON_IsString(c)) { cJSON_Delete(cmd); continue; }

        if (strcmp(c->valuestring, "press") == 0) {
            cJSON *iid = cJSON_GetObjectItem(cmd, "instance_id");
            if (cJSON_IsString(iid))
                set_button(iid->valuestring, 1);
        } else if (strcmp(c->valuestring, "release") == 0) {
            cJSON *iid = cJSON_GetObjectItem(cmd, "instance_id");
            if (cJSON_IsString(iid))
                set_button(iid->valuestring, 0);
        } else if (strcmp(c->valuestring, "quit") == 0) {
            running = 0;
        }

        cJSON_Delete(cmd);
    }
}

/* ── main ────────────────────────────────────────────────────── */

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <sim_config.json>\n", argv[0]);
        return 1;
    }

    /* Disable stdout buffering so JSON events are delivered immediately */
    setvbuf(stdout, NULL, _IONBF, 0);

    char elf_path[2048];
    if (load_config(argv[1], elf_path, sizeof(elf_path)) < 0)
        return 1;

    /* Load ELF */
    elf_firmware_t fw;
    memset(&fw, 0, sizeof(fw));
    if (elf_read_firmware(elf_path, &fw) != 0) {
        emit_error("failed to load ELF file");
        return 1;
    }

    /* Create AVR instance */
    avr = avr_make_mcu_by_name("atmega328p");
    if (!avr) {
        emit_error("failed to create atmega328p instance");
        return 1;
    }
    avr_init(avr);
    avr->frequency = 8000000;   /* 8 MHz internal oscillator */
    avr_load_firmware(avr, &fw);

    /* Attach peripherals */
    attach_peripherals();

    /* Attach UART0 output IRQ to capture Serial.print() */
    avr_irq_t *uart_irq = avr_io_getirq(avr,
        AVR_IOCTL_UART_GETIRQ('0'), UART_IRQ_OUTPUT);
    if (uart_irq)
        avr_irq_register_notify(uart_irq, uart_output_hook, NULL);

    /* Start AVR in background thread */
    pthread_t tid;
    if (pthread_create(&tid, NULL, avr_run_thread, NULL) != 0) {
        emit_error("failed to start AVR thread");
        return 1;
    }

    /* Give the MCU a moment to initialise (boot through setup()) */
    usleep(200000);  /* 200 ms wall-clock */
    if (running)
        emit_event("boot_ok", NULL);

    /* Read commands from stdin until quit or EOF */
    process_commands();

    /* Teardown */
    running = 0;
    pthread_join(tid, NULL);

    return 0;
}
