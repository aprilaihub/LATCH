# Apply one in-memory experimental clock and report baseline-only timing facts.
# argv: DCP TARGET_KIND TARGET_NAME PERIOD_NS OUTPUT

if {$argc != 5} { error "usage: wp014h_synthetic_timing_probe.tcl DCP TARGET_KIND TARGET_NAME PERIOD_NS OUTPUT" }
set dcp [lindex $argv 0]
set target_kind [lindex $argv 1]
set target_name [lindex $argv 2]
set period [lindex $argv 3]
set out [lindex $argv 4]

open_checkpoint $dcp
if {$target_kind eq "port"} {
    set target [get_ports -quiet $target_name]
} elseif {$target_kind eq "pin"} {
    set target [get_pins -quiet $target_name]
} else {
    error "unsupported target kind"
}
if {[llength $target] != 1} { error "synthetic clock target is not unique" }
set existing [get_clocks -quiet wp014h_synth_clock]
if {[llength $existing] == 0} {
    create_clock -name wp014h_synth_clock -period $period $target
} elseif {[llength $existing] != 1 || abs(double([get_property PERIOD $existing]) - double($period)) > 0.000001} {
    error "existing synthetic clock does not match requested period"
}

set seq_cells [get_cells -quiet -hierarchical -filter {IS_SEQUENTIAL == 1}]
set clock_pins [get_pins -quiet -of_objects $seq_cells -filter {IS_CLOCK == 1}]
set timed_clock_pins 0
set target_timed_clock_pins 0
set unassociated_clock_pins 0
foreach pin $clock_pins {
    set pin_clocks [get_clocks -quiet -of_objects $pin]
    if {[llength $pin_clocks] > 0} {
        incr timed_clock_pins
    } else {
        incr unassociated_clock_pins
    }
    if {[llength $pin_clocks] > 0 && [lsearch -exact [get_property NAME $pin_clocks] wp014h_synth_clock] >= 0} {
        incr target_timed_clock_pins
    }
}
set synth_clock [get_clocks -quiet wp014h_synth_clock]
set paths [get_timing_paths -quiet -setup -max_paths 1 -nworst 1 -to $synth_clock]
set slack "unknown"
set data_path_delay "unknown"
set path_startpoint "unknown"
set path_endpoint "unknown"
if {[llength $paths] == 1} {
    set slack [get_property SLACK $paths]
    catch {set data_path_delay [get_property DATAPATH_DELAY $paths]}
    catch {set path_startpoint [get_property STARTPOINT_PIN $paths]}
    catch {set path_endpoint [get_property ENDPOINT_PIN $paths]}
}
set route_text [report_route_status -return_string]
regsub -all {\r?\n} $route_text {;} route_text
set check_text [check_timing -override_defaults no_clock -return_string]
set no_clock_pins "unknown"
if {[regexp {There are ([0-9]+) register/latch pins with no clock} $check_text -> count]} {
    set no_clock_pins $count
}
regsub -all {\r?\n} $check_text {;} check_text

set fh [open $out w]
puts $fh "DOF_SYNTH_TIMING_PROBE_V1"
puts $fh "period_ns=$period"
puts $fh "target_kind=$target_kind"
puts $fh "target_name=$target_name"
puts $fh "sequential_cells=[llength $seq_cells]"
puts $fh "clock_pins=[llength $clock_pins]"
puts $fh "timed_clock_pins=$timed_clock_pins"
puts $fh "target_timed_clock_pins=$target_timed_clock_pins"
puts $fh "target_path_count=[llength $paths]"
puts $fh "no_clock_pins=$no_clock_pins"
puts $fh "unassociated_clock_pins=$unassociated_clock_pins"
puts $fh "timing_path_count=[llength $paths]"
puts $fh "wns_ns=$slack"
puts $fh "data_path_delay_ns=$data_path_delay"
puts $fh "path_startpoint=$path_startpoint"
puts $fh "path_endpoint=$path_endpoint"
puts $fh "route_status=$route_text"
puts $fh "check_timing_no_clock=$check_text"
close $fh
close_design
exit 0
