# Read-only WP-014 diagnostic: capture the authoritative Vivado 2025.1
# command surfaces used by the frozen typed P/G interface.
foreach command {place_design route_design phys_opt_design} {
  puts "DOF_HELP_BEGIN:$command"
  puts [help $command]
  puts "DOF_HELP_END:$command"
}
exit
