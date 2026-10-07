# bash completion for blanket
_blanket() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "off on toggle status idle list doctor help" -- "$cur") )
        return
    fi

    case "${COMP_WORDS[1]}" in
        status)
            COMPREPLY=( $(compgen -W "--verbose -v" -- "$cur") )
            ;;
        off)
            COMPREPLY=( $(compgen -W "--force -f" -- "$cur") )
            ;;
        idle)
            COMPREPLY=( $(compgen -W "off status 60 300 600 900" -- "$cur") )
            ;;
    esac
}

complete -F _blanket blanket
