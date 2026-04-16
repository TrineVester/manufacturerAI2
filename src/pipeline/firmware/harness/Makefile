# simavr harness — build with: make
#
# Prerequisites (Debian/Ubuntu):
#   sudo apt-get install libsimavr-dev libelf-dev libcjson-dev
# Or build simavr from source: https://github.com/buserror/simavr

CC      ?= gcc
CFLAGS  := -Wall -Wextra -O2 -std=c11 -pthread -I/usr/include/simavr
LDFLAGS := -lsimavr -lsimavrparts -lelf -lcjson -lpthread

TARGET  := sim_harness
SRC     := sim_harness.c

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGET)
