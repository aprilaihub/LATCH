# Read-only clock-topology census for an unconstrained FPGA'24 DCP.
# argv: DCP OUTPUT

if {$argc != 2} { error "usage: wp014h_synthetic_clock_topology.tcl DCP OUTPUT" }
set dcp [lindex $argv 0]
set out [lindex $argv 1]

proc esc {value} {
    return [string map [list "%" "%25" "|" "%7C" "\n" "%0A" "\r" "%0D"] $value]
}

open_checkpoint $dcp
set seq_cells [get_cells -quiet -hierarchical -filter {IS_SEQUENTIAL == 1}]
set clock_pins [get_pins -quiet -of_objects $seq_cells -filter {IS_CLOCK == 1}]

# Query the union of timing-graph startpoints once.  Calling all_fanin for
# every hierarchical net segment is both slow and semantically wrong: a
# single clock tree is represented by thousands of local net names.
set starts {}
catch {set starts [all_fanin -flat -startpoints_only -to $clock_pins]}
set rows {}
foreach start $starts {
    set endpoints {}
    catch {set endpoints [all_fanout -flat -endpoints_only -from $start]}
    set sink_names {}
    foreach endpoint $endpoints {
        set class ""
        catch {set class [get_property CLASS $endpoint]}
        if {$class ne "pin"} { continue }
        set is_clock 0
        catch {set is_clock [get_property IS_CLOCK $endpoint]}
        if {$is_clock} { lappend sink_names [get_property NAME $endpoint] }
    }
    set sink_names [lsort -unique -dictionary $sink_names]
    if {[llength $sink_names] == 0} { continue }

    set class ""
    catch {set class [get_property CLASS $start]}
    set source_ports {}
    set drivers {}
    set clocking_cells {}
    if {$class eq "port"} {
        set identity "port:[get_property NAME $start]"
        lappend source_ports [get_property NAME $start]
    } elseif {$class eq "pin"} {
        set cell [get_cells -quiet -of_objects $start]
        set ref ""
        if {[llength $cell] == 1} { set ref [get_property REF_NAME $cell] }
        set row "[get_property NAME $start]@[esc $ref]"
        lappend drivers $row
        if {[regexp -nocase {(MMCM|PLL)} $ref]} {
            lappend clocking_cells "[get_property NAME $cell]@[esc $ref]"
            set identity "generated:$row"
        } elseif {[regexp -nocase {^(BUFG|BUFH|BUFR)} $ref]} {
            set identity "buffer:$row"
        } else {
            set identity "unresolved:$row"
        }
    } else {
        set identity "unresolved:[get_property NAME $start]"
    }
    # Retain the canonical, sorted sink set until all startpoints have been
    # compared.  Some differential input ports are distinct startpoints for
    # the exact same sequential clock domain.  Counts alone are insufficient:
    # domains are merged below only when their complete sink-name lists are
    # byte-for-byte equal.
    lappend rows [list $identity [llength $sink_names] $source_ports $drivers $clocking_cells $sink_names]
}

# Assign deterministic domain ids after sorting by source identity.  This is
# an exact set-equivalence operation over Vivado-discovered clock pins, not a
# port-name heuristic or a QoR-derived choice.
set domains {}
set annotated_rows {}
foreach row [lsort -dictionary -index 0 $rows] {
    set sink_names [lindex $row 5]
    set domain_id ""
    set domain_index 0
    foreach existing $domains {
        if {$sink_names eq $existing} {
            set domain_id [format "domain:%04d" $domain_index]
            break
        }
        incr domain_index
    }
    if {$domain_id eq ""} {
        lappend domains $sink_names
        set domain_id [format "domain:%04d" [expr {[llength $domains] - 1}]]
    }
    lset row 5 $domain_id
    lappend annotated_rows $row
}

set fh [open $out w]
puts $fh "DOF_SYNTH_TOPOLOGY_V2"
puts $fh "part=[esc [get_property PART [current_design]]]"
puts $fh "sequential_cells=[llength $seq_cells]"
puts $fh "clock_pins=[llength $clock_pins]"
foreach row $annotated_rows {
    set identity [lindex $row 0]
    set sinks [lindex $row 1]
    set ports [lindex $row 2]
    set drivers [lindex $row 3]
    set clocking [lindex $row 4]
    set domain_id [lindex $row 5]
    puts $fh "candidate=[esc $identity]|$sinks|[esc [join $ports ,]]|[esc [join $drivers ,]]|[esc [join $clocking ,]]|[esc $domain_id]"
}
close $fh
close_design
exit 0
