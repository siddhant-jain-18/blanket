# fish completion for blanket
complete -c blanket -f

complete -c blanket -n '__fish_use_subcommand' -a off -d 'Blank the screen'
complete -c blanket -n '__fish_use_subcommand' -a on -d 'Unblank the screen'
complete -c blanket -n '__fish_use_subcommand' -a toggle -d 'Toggle between on and off'
complete -c blanket -n '__fish_use_subcommand' -a status -d 'Show the display power state'
complete -c blanket -n '__fish_use_subcommand' -a idle -d 'Blank after N seconds of inactivity'
complete -c blanket -n '__fish_use_subcommand' -a list -d 'List input devices seen by the watcher'
complete -c blanket -n '__fish_use_subcommand' -a doctor -d 'Diagnose common setup problems'
complete -c blanket -n '__fish_use_subcommand' -a help -d 'Show help'

complete -c blanket -n '__fish_seen_subcommand_from status' -l verbose -s v -d 'Show watcher details'
complete -c blanket -n '__fish_seen_subcommand_from off' -l force -s f -d 'Blank even if the watcher is not running'
complete -c blanket -n '__fish_seen_subcommand_from idle' -a 'off status 60 300 600 900'