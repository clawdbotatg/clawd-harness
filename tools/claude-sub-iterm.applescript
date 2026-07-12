-- Desktop launcher for tools/subpick.py, in iTerm2 instead of Terminal.app.
-- A .command file always opens in Terminal, so the iTerm variant is a tiny
-- AppleScript app. Rebuild with:
--   osacompile -o ~/Desktop/"Claude Sub.app" tools/claude-sub-iterm.applescript
-- First launch asks once for permission to control iTerm (macOS Automation).
on run
	tell application "iTerm"
		activate
		set w to missing value
		try
			set w to (create window with default profile)
		on error
			-- cold start may have already opened the initial window
			if (count of windows) > 0 then set w to current window
		end try
		if w is missing value then set w to (create window with default profile)
		tell current session of w
			write text "exec /usr/bin/python3 /Users/austingriffith/clawd/clawd-harness/tools/subpick.py"
		end tell
	end tell
end run
