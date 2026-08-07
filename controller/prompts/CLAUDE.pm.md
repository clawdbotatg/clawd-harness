# PM home

This directory is the working home of the fleet project-manager brain (the
clawd-harness controller). It is deliberately small: the PM's real instructions
arrive per turn as a system prompt (controller/prompts/private.md in the
clawd-harness repo — edit THAT, not this file; this copy is reinstalled from
controller/prompts/CLAUDE.pm.md at every controller boot).

Ground rules that belong to this home:
- There is no project code here. Fleet state lives behind the `fleet` MCP
  tools; use them, not the filesystem.
- Do not write files here. Deliverables are tool calls and chat replies.
