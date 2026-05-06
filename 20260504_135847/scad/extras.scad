// ============================================================
// Extra parts — separate build plate
// ============================================================

hatch_w = 25.400;
hatch_l = 45.900;
hatch_t = 1.500;
loop_w = 4.000;
loop_h = 3.000;
loop_t = 1.000;
tab_d = 0.800;
tab_h = 1.500;
slit_w = 5.000;
arm_gap = 2.000;
bend_r = 1.500;

module spring_latch() {
  cube([loop_w, loop_t, loop_h]);
  translate([0, -tab_d, hatch_t])
    cube([loop_w, tab_d + loop_t, tab_h]);
  translate([loop_w/2, loop_t + arm_gap/2, loop_h])
    rotate([90, 0, 90])
      rotate_extrude(angle=180, $fn=32)
        translate([bend_r, 0, 0])
          square([loop_t, loop_w], center=true);
  translate([0, loop_t + arm_gap, 0])
    cube([loop_w, loop_t, loop_h]);
}

// button
translate([10.002, 0, 0]) {
  // Custom button: btn_power
  // Socket (snaps onto switch cylinder Ø3.40mm)
  difference() {
    cylinder(h = 1.210, r = 2.850, $fn = 32);
    cylinder(h = 1.210, r = 1.850, $fn = 32);
  }
  // Stem (passes through cavity + ceiling)
  translate([0, 0, 1.210])
    linear_extrude(height = 10.100)
      polygon(points = [[-4.700, -3.700], [-4.700, 3.700], [4.700, 3.700], [4.700, -3.700]]);
  // Cap
  translate([0, 0, 11.310])
    linear_extrude(height = 1.500)
      polygon(points = [[10.000, 0.000], [9.950, -0.980], [9.810, -1.950], [9.570, -2.900], [9.240, -3.830], [8.820, -4.710], [8.310, -5.560], [7.730, -6.340], [7.070, -7.070], [6.340, -7.730], [5.560, -8.310], [4.710, -8.820], [3.830, -9.240], [2.900, -9.570], [1.950, -9.810], [0.980, -9.950], [0.000, -10.000], [-0.980, -9.950], [-1.950, -9.810], [-2.900, -9.570], [-3.830, -9.240], [-4.710, -8.820], [-5.560, -8.310], [-6.340, -7.730], [-7.070, -7.070], [-7.730, -6.340], [-8.310, -5.560], [-8.820, -4.710], [-9.240, -3.830], [-9.570, -2.900], [-9.810, -1.950], [-9.950, -0.980], [-10.000, 0.000], [-9.950, 0.980], [-9.810, 1.950], [-9.570, 2.900], [-9.240, 3.830], [-8.820, 4.710], [-8.310, 5.560], [-7.730, 6.340], [-7.070, 7.070], [-6.340, 7.730], [-5.560, 8.310], [-4.710, 8.820], [-3.830, 9.240], [-2.900, 9.570], [-1.950, 9.810], [-0.980, 9.950], [0.000, 10.000], [0.980, 9.950], [1.950, 9.810], [2.900, 9.570], [3.830, 9.240], [4.710, 8.820], [5.560, 8.310], [6.340, 7.730], [7.070, 7.070], [7.730, 6.340], [8.310, 5.560], [8.820, 4.710], [9.240, 3.830], [9.570, 2.900], [9.810, 1.950], [9.950, 0.980]]);
}

// battery hatch
translate([35.705, 0, 0]) {
  difference() {
    cube([hatch_w, hatch_l, hatch_t]);
    translate([(hatch_w - slit_w) / 2, 0, -1])
      cube([slit_w, 3.000, hatch_t + 2]);
  }
  
  difference() {
    translate([(hatch_w - loop_w) / 2, 0, 0])
      spring_latch();
    intersection() {
      translate([6.700, -1, 7.200])
        rotate([-90, 0, 0])
          cylinder(h = 47.900, r = 5.200, $fn = 32);
      translate([1.500, -1, 0])
        cube([10.400, 47.900, 7.200]);
    }
    intersection() {
      translate([18.700, -1, 7.200])
        rotate([-90, 0, 0])
          cylinder(h = 47.900, r = 5.200, $fn = 32);
      translate([13.500, -1, 0])
        cube([10.400, 47.900, 7.200]);
    }
  }
  
  translate([10.700, hatch_l, hatch_t])
    cube([4.000, tab_d, tab_h]);
  
  difference() {
    translate([1.500, 0, 1.500])
      cube([22.400, 45.900, 5.700]);
    translate([10.000, -1, 0])
      cube([5.400, 6.500, 8.200]);
    intersection() {
      translate([6.700, -1, 7.200])
        rotate([-90, 0, 0])
          cylinder(h = 47.900, r = 5.200, $fn = 32);
      translate([1.500, -1, 0])
        cube([10.400, 47.900, 7.200]);
    }
    intersection() {
      translate([18.700, -1, 7.200])
        rotate([-90, 0, 0])
          cylinder(h = 47.900, r = 5.200, $fn = 32);
      translate([13.500, -1, 0])
        cube([10.400, 47.900, 7.200]);
    }
  }
}
