---
name: iron-todo
description: The shared to-do list of the IRON this project belongs to (an "iron in the fire" = a named group of projects in the harness). Use when the user says "put that on the iron", "add it to the iron's list", "what's left on this iron / what's open here", or wants to check something off the iron. NOT the personal todo list (that's the `todo` skill) — never move items between the two. Also used at a 📑 wrap to record what is still open.
---

# The iron's to-do list

Every project in the harness can sit in one **iron** — a named group of
projects that make up one effort. Each iron carries one shared to-do list of
what's still open across the whole effort. The operator sees it as the ☑
panel over the terminal on any session in that iron, and on the iron's page;
every session in the iron, on any machine, sees the same list. It is separate
from Austin's personal list (`todo`, todo.atg.link): an iron's eight
follow-ups stay on the iron.

Talk to it with the `harness-todo` CLI (on PATH inside every harness session;
the harness resolves the iron from this session's project):

```bash
harness-todo                  # open items   ([ ] <id>  <text>), oldest first
harness-todo list --all       # include done items ([x])
harness-todo add <text>       # add one item — plain words, no quotes needed
harness-todo done <id|words>  # check off (a unique few words of the item work)
harness-todo undone <id>      # reopen
harness-todo rm <id|words>    # delete
harness-todo clear            # purge done items
```

If it answers that the project **isn't in any iron**, there is no list to
write to: say so and stop (don't fall back to the personal `todo` list).
If `HARNESS_TODO_URL` is unset you're not running under the harness.

## How to behave

- **Adding**: short imperative items ("test scrollback on phone"), one per
  task. Name the project if it isn't obvious — the list spans several
  projects. Only add when asked, or at a wrap; never dump every follow-up you
  can think of onto the iron.
- **Checking off**: `harness-todo done` with a distinctive word is enough; if
  it says ambiguous, list and use the id.
- **Reading**: `harness-todo` prints open items in the operator's order (he
  drag-orders in the app; `order` is his, not yours). When asked "what's
  left", show the list and, if you have context, say which item fits now.
- Confirm every write with the CLI's one-line output.
