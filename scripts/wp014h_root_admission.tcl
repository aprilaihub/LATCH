if {$argc != 2} { error "usage: wp014h_root_admission.tcl DCP OUT" }
set dcp [lindex $argv 0]
set out [lindex $argv 1]
set status OK
set error_class ""
if {[catch {open_checkpoint $dcp} message]} {
  set status OPEN_FAILED
  set error_class [lindex [split $message ":"] 0]
}
set fh [open $out w]
puts $fh "WP014H_ROOT_ADMISSION_V1"
puts $fh "status=$status"
puts $fh "error_class=$error_class"
if {$status eq "OK"} {
  puts $fh "part=[get_property PART [current_design]]"
  set clocks [lsort [get_clocks -quiet]]
  puts $fh "clock_count=[llength $clocks]"
  foreach clk $clocks {
    puts $fh "clock=[get_property NAME $clk]|[get_property PERIOD $clk]"
  }
  set routes [report_route_status -return_string]
  regsub -all {\r?\n} $routes {;} routes
  puts $fh "route_status=$routes"
}
close $fh
if {$status ne "OK"} { exit 2 }
exit 0
