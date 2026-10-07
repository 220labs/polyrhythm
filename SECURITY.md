# Security

polyrhythm runs with **Full Disk Access** and **Automation → Messages**, granted to one
ad-hoc-signed launcher (`polyrhythm-host`) that runs exactly one script, compiled into it, with
Python isolated (`-I`). It reads one iMessage chat, chosen by SQL; by default only the owner's
own rows leave the database, and in a 1:1 thread only those that contain `ai` followed by `:`
or `,` (a substring match) or the sign-off. The README's "Security model" section has the details and the limits.

**Report a vulnerability privately**: on this repository, Security → Report a vulnerability.
Please don't open a public issue for it. Supported: the latest `main`.

Before you apply any update, check two things in `git log -p`: nothing grants Full Disk Access
or Automation to `python3` or Terminal, and every change to `polyrhythm-host.c` reads as what
it claims to be.
