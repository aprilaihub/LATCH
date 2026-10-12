# Read-only common observation/query feasibility probe for WP-012-02.
# Usage: vivado -mode batch -nolog -nojournal -notrace -source this.tcl -tclargs ROOT_DCP

if {$argc != 1} {
    puts stderr "usage: wp012_02_readonly_probe.tcl ROOT_DCP"
    exit 2
}

proc emit {name started status value} {
    set latency [expr {[clock milliseconds] - $started}]
    set encoded [binary encode base64 -maxlen 0 $value]
    puts "WP01202_FIELD\t$name\t$latency\t$status\t$encoded"
}

proc probe {name script} {
    set started [clock milliseconds]
    set code [catch {uplevel 1 $script} value options]
    # Multi-command probes use `return` to provide their result; at the
    # top-level uplevel that is Tcl completion code 2, not a query failure.
    if {$code != 0 && $code != 2} {
        emit $name $started ERROR $value
    } else {
        emit $name $started OK $value
    }
}

set root [file normalize [lindex $argv 0]]
set open_started [clock milliseconds]
if {[catch {open_checkpoint $root} open_error]} {
    emit open_checkpoint $open_started ERROR $open_error
    exit 3
}
emit open_checkpoint $open_started OK "opened"

probe tool_version {version -short}
probe part {get_property PART [current_design]}
probe clocks {
    set rows {}
    foreach item [get_clocks -quiet] {
        lappend rows [list [get_property NAME $item] [get_property PERIOD $item]]
    }
    return $rows
}
probe timing_summary {report_timing_summary -return_string -max_paths 10 -nworst 1}
probe critical_paths {
    set rows {}
    foreach path [get_timing_paths -quiet -max_paths 10 -nworst 1] {
        set startpin [get_property STARTPOINT_PIN $path]
        set endpin [get_property ENDPOINT_PIN $path]
        lappend rows [list \
            [get_property SLACK $path] \
            [get_property DATAPATH_DELAY $path] \
            [get_property LOGIC_LEVELS $path] \
            [get_property NAME $startpin] \
            [get_property NAME $endpin]]
    }
    return $rows
}
probe critical_object_locations {
    set rows {}
    foreach path [get_timing_paths -quiet -max_paths 10 -nworst 1] {
        foreach property {STARTPOINT_PIN ENDPOINT_PIN} {
            set pin [get_property $property $path]
            set cells [get_cells -quiet -of_objects $pin]
            foreach cell $cells {
                lappend rows [list $property [get_property NAME $cell] [get_property LOC $cell] [get_property REF_NAME $cell]]
            }
        }
    }
    return $rows
}
probe utilization {report_utilization -return_string}
probe route_status {report_route_status -return_string}
probe congestion {report_design_analysis -congestion -return_string}
probe high_fanout_nets {
    set rows {}
    set nets [get_nets -quiet -hierarchical -filter {FLAT_PIN_COUNT >= 64}]
    foreach net [lrange $nets 0 99] {
        lappend rows [list [get_property NAME $net] [get_property FLAT_PIN_COUNT $net]]
    }
    return $rows
}
probe slice_geometry {
    set names [get_property NAME [get_sites -quiet -filter {SITE_TYPE =~ SLICE*}]]
    return [list [llength $names] [lindex $names 0] [lindex $names end]]
}
probe clock_regions {get_property NAME [get_clock_regions -quiet]}
probe slrs {get_property NAME [get_slrs -quiet]}
probe pblocks {
    set rows {}
    foreach pblock [get_pblocks -quiet] {
        lappend rows [list [get_property NAME $pblock] [get_property GRID_RANGES $pblock]]
    }
    return $rows
}
probe constraints_summary {
    return [list \
        clocks [llength [get_clocks -quiet]] \
        pblocks [llength [get_pblocks -quiet]]]
}
set close_started [clock milliseconds]
close_design
emit close_design $close_started OK "closed"
exit 0
