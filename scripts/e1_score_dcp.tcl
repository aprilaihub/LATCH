# Fixed read-only Protocol E1-v1 scorer probe.
# argv: input DCP, optional previously frozen target clock.

if {$argc < 1 || $argc > 2} {
    puts stderr "usage: e1_score_dcp.tcl INPUT_DCP ?TARGET_CLOCK?"
    exit 64
}

set input_dcp [lindex $argv 0]
open_checkpoint $input_dcp

if {$argc == 2} {
    set target_name [lindex $argv 1]
    set target_clock [get_clocks -quiet $target_name]
    if {[llength $target_clock] != 1} {
        puts stderr "DOF_E1_ERROR:frozen_target_clock_not_unique"
        close_design
        exit 65
    }
    set target_rule "frozen_from_root"
} else {
    set contest_clock [get_clocks -quiet clk_fpl26contest]
    if {[llength $contest_clock] == 1} {
        set target_clock $contest_clock
        set target_name [get_property NAME $target_clock]
        set target_rule "contest_clock_name"
    } else {
        set worst_path [get_timing_paths -max_paths 1 -setup]
        if {[llength $worst_path] != 1} {
            puts stderr "DOF_E1_ERROR:worst_setup_path_not_unique"
            close_design
            exit 66
        }
        set target_name [get_property ENDPOINT_CLOCK $worst_path]
        set target_clock [get_clocks -quiet $target_name]
        if {[llength $target_clock] != 1} {
            puts stderr "DOF_E1_ERROR:endpoint_clock_not_unique"
            close_design
            exit 67
        }
        set target_rule "endpoint_clock_of_worst_setup_path"
    }
}

set period [get_property PERIOD $target_clock]
set target_path [get_timing_paths -max_paths 1 -setup -to $target_clock]
if {[llength $target_path] != 1} {
    puts stderr "DOF_E1_ERROR:target_setup_path_not_unique"
    close_design
    exit 68
}
set wns [get_property SLACK $target_path]
set route_status [report_route_status -return_string]
set timing_summary [report_timing_summary -delay_type max -max_paths 10 -return_string]

puts "DOF_E1_TARGET_CLOCK:$target_name"
puts "DOF_E1_TARGET_RULE:$target_rule"
puts "DOF_E1_TARGET_PERIOD_NS:$period"
puts "DOF_E1_TARGET_WNS_NS:$wns"
puts "DOF_E1_TIMING_SUMMARY_BEGIN"
puts $timing_summary
puts "DOF_E1_TIMING_SUMMARY_END"
puts "DOF_E1_ROUTE_STATUS_BEGIN"
puts $route_status
puts "DOF_E1_ROUTE_STATUS_END"
close_design
exit 0
