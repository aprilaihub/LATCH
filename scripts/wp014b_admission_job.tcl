proc wp014b_b64 {value} {return [binary encode base64 -maxlen 0 [encoding convertto utf-8 $value]]}
proc wp014b_emit {fd fields} {puts $fd [join $fields "\t"]; flush $fd}
proc wp014b_heavy {fd label script} {
    wp014b_emit $fd [list "WP014B_HEAVY_START" $label [clock milliseconds]]
    set status [catch {uplevel 1 $script} detail]
    wp014b_emit $fd [list "WP014B_HEAVY_END" $label [clock milliseconds] $status [wp014b_b64 $detail]]
    if {$status != 0} {return -code error $detail}
    return $detail
}
proc wp014b_qor {fd phase} {
    set path_status [catch {get_timing_paths -max_paths 1 -nworst 1} paths]
    set wns ""; set delay ""
    if {$path_status == 0 && [llength $paths] > 0} {
        set path [lindex $paths 0]
        set wns [get_property SLACK $path]
        set delay [get_property DATAPATH_DELAY $path]
    }
    set timing_status [catch {report_timing_summary -return_string -max_paths 1} timing]
    set route_status [catch {report_route_status -return_string} route]
    wp014b_emit $fd [list "WP014B_QOR" $phase $path_status $wns $delay $timing_status [wp014b_b64 $timing] $route_status [wp014b_b64 $route]]
}
proc wp014b_target {fd kind objects} {
    wp014b_emit $fd [list "WP014B_TARGET" $kind [llength $objects] [wp014b_b64 [join $objects "\n"]]]
}

if {[llength $argv] != 5} {puts stderr "usage ROOT OUT KIND VALUE CHILD"; exit 64}
lassign $argv root_dcp output_tsv action_kind action_value child_dcp
set fd [open $output_tsv w]; fconfigure $fd -encoding utf-8 -translation lf
set open_status [catch {open_checkpoint $root_dcp} open_detail]
wp014b_emit $fd [list "WP014B_OPEN" $open_status [wp014b_b64 $open_detail]]
if {$open_status != 0} {close $fd; exit 2}
wp014b_emit $fd [list "WP014B_PART" [get_property PART [current_design]]]
wp014b_qor $fd before
set action_started [clock milliseconds]
set action_status [catch {
    switch -- $action_kind {
        route_directive {
            route_design -unroute
            wp014b_heavy $fd "route_design -directive $action_value" [list route_design -directive $action_value]
        }
        place_directive {
            route_design -unroute; place_design -unplace
            wp014b_heavy $fd "place_design -directive $action_value" [list place_design -directive $action_value]
            wp014b_heavy $fd "route_design -directive Default" {route_design -directive Default}
        }
        place_net_delay_weight {
            route_design -unroute; place_design -unplace
            wp014b_heavy $fd "place_design -net_delay_weight $action_value" [list place_design -net_delay_weight $action_value]
            wp014b_heavy $fd "route_design -directive Default" {route_design -directive Default}
        }
        place_directive_net_delay {
            lassign [split $action_value ,] directive weight
            route_design -unroute; place_design -unplace
            wp014b_heavy $fd "place_design -directive $directive -net_delay_weight $weight" [list place_design -directive $directive -net_delay_weight $weight]
            wp014b_heavy $fd "route_design -directive Default" {route_design -directive Default}
        }
        phys_directive {wp014b_heavy $fd "phys_opt_design -directive $action_value" [list phys_opt_design -directive $action_value]}
        phys_option {wp014b_heavy $fd "phys_opt_design -$action_value" [list phys_opt_design -$action_value]}
        phys_option_placed {
            route_design -unroute
            wp014b_heavy $fd "phys_opt_design -$action_value" [list phys_opt_design -$action_value]
            wp014b_heavy $fd "route_design -directive Default" {route_design -directive Default}
        }
        phys_option_combo {
            lassign [split $action_value ,] option_a option_b
            wp014b_heavy $fd "phys_opt_design -$option_a -$option_b" [list phys_opt_design -$option_a -$option_b]
        }
        phys_force_replication {
            set targets [lrange [get_nets -hierarchical -filter {FLAT_PIN_COUNT >= 64}] 0 0]
            wp014b_target $fd net $targets
            if {[llength $targets] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            wp014b_heavy $fd "phys_opt_design -force_replication_on_nets <NET>" [list phys_opt_design -force_replication_on_nets $targets]
        }
        phys_force_replication_placed {
            set targets [lrange [get_nets -hierarchical -filter {FLAT_PIN_COUNT >= 64}] 0 0]
            wp014b_target $fd net $targets
            if {[llength $targets] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            route_design -unroute
            wp014b_heavy $fd "phys_opt_design -force_replication_on_nets <NET>" [list phys_opt_design -force_replication_on_nets $targets]
            wp014b_heavy $fd "route_design -directive Default" {route_design -directive Default}
        }
        targeted_route_delay {
            set targets [lrange [get_nets -hierarchical -filter {FLAT_PIN_COUNT >= 16}] 0 0]
            wp014b_target $fd net $targets
            if {[llength $targets] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            route_design -unroute -nets $targets
            wp014b_heavy $fd "route_design -nets <NET> -delay" [list route_design -nets $targets -delay]
        }
        property_cell_loc_fixed {
            set candidates [get_cells -hierarchical -filter {IS_PRIMITIVE && LOC != {}}]
            set targets [lrange $candidates 0 0]; wp014b_target $fd cell $targets
            if {[llength $targets] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set_property IS_LOC_FIXED true $targets
            if {![get_property IS_LOC_FIXED $targets]} {error "WP014B_MUTATION_NOT_OBSERVED"}
        }
        property_net_dont_touch {
            set targets [lrange [get_nets -hierarchical -filter {FLAT_PIN_COUNT >= 16}] 0 0]
            wp014b_target $fd net $targets
            if {[llength $targets] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set_property DONT_TOUCH true $targets
            if {![get_property DONT_TOUCH $targets]} {error "WP014B_MUTATION_NOT_OBSERVED"}
        }
        property_site_prohibit {
            set targets [lrange [get_sites -filter {SITE_TYPE =~ SLICE* && PROHIBIT == 0}] 0 0]
            wp014b_target $fd site $targets
            if {[llength $targets] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set_property PROHIBIT true $targets
            if {![get_property PROHIBIT $targets]} {error "WP014B_MUTATION_NOT_OBSERVED"}
        }
        pblock_exclude_placement {
            set regions [lrange [get_clock_regions] 0 0]; wp014b_target $fd clock_region $regions
            if {[llength $regions] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set sites [get_sites -of_objects $regions]
            if {[llength $sites] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set pb [create_pblock wp014b_probe]; resize_pblock $pb -add $sites
            set_property EXCLUDE_PLACEMENT true $pb
            if {![get_property EXCLUDE_PLACEMENT $pb]} {error "WP014B_MUTATION_NOT_OBSERVED"}
        }
        pblock_assign_cell {
            set cells [lrange [get_cells -hierarchical -filter {IS_PRIMITIVE && LOC != {}}] 0 0]
            wp014b_target $fd cell $cells
            if {[llength $cells] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set sites [get_sites -of_objects $cells]
            set regions [get_clock_regions -of_objects $sites]
            if {[llength $regions] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set region_sites [get_sites -of_objects [lindex $regions 0]]
            if {[llength $region_sites] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set pb [create_pblock wp014b_probe]; resize_pblock $pb -add $region_sites
            add_cells_to_pblock $pb $cells
            if {[llength [get_cells -of_objects $pb]] == 0} {error "WP014B_MUTATION_NOT_OBSERVED"}
        }
        place_cell_roundtrip {
            set cells [lrange [get_cells -hierarchical -filter {IS_PRIMITIVE && BEL != {}}] 0 0]
            wp014b_target $fd cell $cells
            if {[llength $cells] == 0} {error "WP014B_NO_APPLICABLE_TARGET"}
            set loc [get_property LOC $cells]; set bel [get_property BEL $cells]
            set bel_leaf [lindex [split $bel .] end]
            set full_bel "$loc/$bel_leaf"
            unplace_cell $cells; place_cell [list $cells $full_bel]
            if {[get_property LOC $cells] ne $loc} {error "WP014B_MUTATION_NOT_OBSERVED"}
        }
        swap_locs {
            set pair {}; set by_ref [dict create]
            foreach cell [get_cells -hierarchical -filter {IS_PRIMITIVE && LOC != {}}] {
                set ref [get_property REF_NAME $cell]
                if {[dict exists $by_ref $ref]} {set pair [list [dict get $by_ref $ref] $cell]; break}
                dict set by_ref $ref $cell
            }
            wp014b_target $fd cell $pair
            if {[llength $pair] != 2} {error "WP014B_NO_APPLICABLE_TARGET"}
            set before_a [get_property LOC [lindex $pair 0]]; set before_b [get_property LOC [lindex $pair 1]]
            swap_locs [lindex $pair 0] [lindex $pair 1]
            if {[get_property LOC [lindex $pair 0]] ne $before_b || [get_property LOC [lindex $pair 1]] ne $before_a} {error "WP014B_MUTATION_NOT_OBSERVED"}
        }
        default {error "WP014B_UNKNOWN_ACTION_KIND"}
    }
} action_detail]
set action_ended [clock milliseconds]
wp014b_emit $fd [list "WP014B_ACTION" $action_kind [wp014b_b64 $action_value] $action_started $action_ended $action_status [wp014b_b64 $action_detail]]
if {$action_status == 0} {
    wp014b_qor $fd after
    set write_status [catch {write_checkpoint -force $child_dcp} write_detail]
    wp014b_emit $fd [list "WP014B_CHILD" $write_status [wp014b_b64 $write_detail]]
} else {
    wp014b_emit $fd [list "WP014B_CHILD" 1 [wp014b_b64 "not written after action failure"]]
}
close_design; close $fd
exit 0
