# `c` — pick the best Claude subscription and launch claude RIGHT HERE.
# Runs subpick's full selector (table + decision + countdown; Enter skips),
# which execs claude under the winning CLAUDE_CONFIG_DIR in the current dir.
# Symlink into autoload so it works in every shell:
#   ln -sf ~/clawd/clawd-harness/tools/c.fish ~/.config/fish/functions/c.fish
# Extra args pass through to claude:  c --resume   c -p "..."
function c --description "claude on the best subscription, in this folder"
    /usr/bin/python3 $HOME/clawd/clawd-harness/tools/subpick.py -- $argv
end
