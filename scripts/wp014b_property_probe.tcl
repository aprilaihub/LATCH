proc wp014b_b64 {value} {
    return [binary encode base64 -maxlen 0 [encoding convertto utf-8 $value]]
}

if {[llength $argv] != 2} {
    puts stderr "usage: wp014b_property_probe.tcl ROOT_DCP OUTPUT_TSV"
    exit 64
}
set root_dcp [lindex $argv 0]
set output_tsv [lindex $argv 1]
set fd [open $output_tsv w]
fconfigure $fd -encoding utf-8 -translation lf

set open_status [catch {open_checkpoint $root_dcp} open_detail]
puts $fd [join [list "WP014B_OPEN" $open_status [wp014b_b64 $open_detail]] "\t"]
if {$open_status != 0} {
    close $fd
    exit 2
}

foreach class_name {cell net pblock site bel} {
    set list_status [catch {list_property -class $class_name} properties]
    puts $fd [join [list "WP014B_CLASS" $class_name $list_status [llength $properties]] "\t"]
    if {$list_status != 0} {
        puts $fd [join [list "WP014B_CLASS_ERROR" $class_name [wp014b_b64 $properties]] "\t"]
        continue
    }
    foreach property_name [lsort -unique $properties] {
        set report_status [catch {
            report_property -all -return_string -class $class_name $property_name
        } report_detail]
        set values_status [catch {
            list_property_value -class $class_name $property_name
        } legal_values]
        puts $fd [join [list \
            "WP014B_PROPERTY" $class_name $property_name $report_status \
            [wp014b_b64 $report_detail] $values_status [wp014b_b64 $legal_values] \
        ] "\t"]
    }
}

set object_queries [dict create \
    cells {get_cells -hierarchical} \
    nets {get_nets -hierarchical} \
    pblocks {get_pblocks} \
    sites {get_sites} \
    bels {get_bels} \
    dsp_cells {get_cells -hierarchical -filter {PRIMITIVE_TYPE =~ DSP.*}} \
    bram_cells {get_cells -hierarchical -filter {PRIMITIVE_TYPE =~ BLOCKRAM.* || PRIMITIVE_TYPE =~ BRAM.*}} \
    uram_cells {get_cells -hierarchical -filter {PRIMITIVE_TYPE =~ URAM.*}} \
]
dict for {object_type command} $object_queries {
    set query_status [catch {uplevel #0 $command} objects]
    set count 0
    if {$query_status == 0} {set count [llength $objects]}
    puts $fd [join [list "WP014B_OBJECT_COUNT" $object_type $query_status $count] "\t"]
}

puts $fd [join [list "WP014B_PART" [get_property PART [current_design]]] "\t"]
puts $fd [join [list "WP014B_DESIGN_MODE" [get_property DESIGN_MODE [current_design]]] "\t"]
puts $fd [join [list "WP014B_ROUTE_STATUS" [get_property ROUTE_STATUS [current_design]]] "\t"]
close_design
close $fd
exit 0
