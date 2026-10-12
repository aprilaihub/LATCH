# Read-only constrained-DCP metadata probe for WP-008 holdout sealing.
# argv: input DCP, documented target clock, documented period in ns.

if {$argc != 3} {
    puts stderr "usage: audit_holdout_dcp.tcl INPUT_DCP TARGET_CLOCK PERIOD_NS"
    exit 64
}

set input_dcp [lindex $argv 0]
set expected_clock_name [lindex $argv 1]
set expected_period [lindex $argv 2]

open_checkpoint $input_dcp

set design_part [get_property PART [current_design]]
set part_object [get_parts -quiet $design_part]
puts "DOF_HOLDOUT_TOOL_VERSION:[version -short]"
puts "DOF_HOLDOUT_PART:$design_part"
foreach property_name {SPEED TEMPERATURE_GRADE FAMILY ARCHITECTURE} {
    set property_value "unknown"
    if {[llength $part_object] == 1} {
        catch {set property_value [get_property $property_name $part_object]}
    }
    puts "DOF_HOLDOUT_PART_${property_name}:$property_value"
}

set clocks [lsort -dictionary [get_clocks -quiet]]
puts "DOF_HOLDOUT_CLOCK_COUNT:[llength $clocks]"
foreach clock_object $clocks {
    set clock_name [get_property NAME $clock_object]
    set clock_period [get_property PERIOD $clock_object]
    puts "DOF_HOLDOUT_CLOCK:$clock_name|$clock_period"
}

set expected_clock [get_clocks -quiet $expected_clock_name]
if {[llength $expected_clock] != 1} {
    puts stderr "DOF_HOLDOUT_ERROR:documented_target_clock_not_unique"
    close_design
    exit 65
}
set observed_period [get_property PERIOD $expected_clock]
if {![string is double -strict $expected_period] || ![string is double -strict $observed_period]} {
    puts stderr "DOF_HOLDOUT_ERROR:target_period_not_numeric"
    close_design
    exit 66
}
if {abs(double($observed_period) - double($expected_period)) > 0.000001} {
    puts stderr "DOF_HOLDOUT_ERROR:documented_target_period_mismatch"
    close_design
    exit 67
}
puts "DOF_HOLDOUT_TARGET_CLOCK:$expected_clock_name"
puts "DOF_HOLDOUT_TARGET_PERIOD_NS:$observed_period"

puts "DOF_HOLDOUT_TIMING_SUMMARY_BEGIN"
puts [report_timing_summary -delay_type min_max -max_paths 10 -return_string]
puts "DOF_HOLDOUT_TIMING_SUMMARY_END"
puts "DOF_HOLDOUT_ROUTE_STATUS_BEGIN"
puts [report_route_status -return_string]
puts "DOF_HOLDOUT_ROUTE_STATUS_END"

close_design
exit 0
