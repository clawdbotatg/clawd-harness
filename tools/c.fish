# `c` — pick the best Claude subscription and launch claude RIGHT HERE.
# Runs subpick's full selector (table + decision + countdown; Enter skips),
# which execs claude under the winning CLAUDE_CONFIG_DIR in the current dir.
# Symlink into autoload so it works in every shell:
#   ln -sf <checkout>/tools/c.fish ~/.config/fish/functions/c.fish
# The checkout is ~/clawd-harness on some boxes, ~/clawd/clawd-harness on others.
# Extra args pass through to claude:  c --resume   c -p "..."
function c --description "claude on the best subscription, in this folder"
    for d in $HOME/clawd-harness $HOME/clawd/clawd-harness
        if test -f $d/tools/subpick.py
            /usr/bin/python3 $d/tools/subpick.py -- $argv
            return
        end
    end
    echo "c: tools/subpick.py not found under $HOME" >&2
    return 1
end
