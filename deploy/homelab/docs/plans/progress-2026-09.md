# seestar-sync k3s relocation — progress ledger

Plan: deploy/homelab/docs/plans/2026-08-12-seestar-sync-k3s-relocation.md
Execution mode: gates win — one task per merge, Brian merges each PR.

Repos are EXTERNAL to this worktree:
  Task 1,2 -> ~/Projects/nas-setup        (BASE f304aec)
  Task 3,4 -> ~/Projects/seestar-backup   (the seestar-sync repo itself)
  Task 5,7,8,9 -> ~/Projects/homelab-cluster
  Task 6   -> ~/Projects/uptime-kuma

Task 1: complete (commits 0ac3f95..3b3cc83, review clean) — AWAITING BRIAN'S MERGE
  Branch claude/nfs-share-loop pushed to nas-setup. No PR opened (agents don't merge).
  Implementer found one thing the brief missed: the GUARD assert's fail_msg still
  referenced the deleted nfs_share_lookup, and ansible-core 2.19 resolves fail_msg
  eagerly even when the assert passes — every run failed until repointed.
  Fix pass addressed 3 review findings (2 stale fail_msg targets, 1 unreachable
  guard message, netboot-singular naming -> generic). Comment/message-only, verified.

  GATE CAVEAT (reviewer's correction to the plan's wording): both runs were
  validate mode, so mutating tasks (when: converge|bool) never executed, and
  "run twice -> changed=0" is near-vacuous there. The empty SURFACE diff proves
  the read/diff/verify path only. Fleet safety rests on a different fact: netboot
  shows NO diff, and the save task is gated on a diff, so netboot's export is
  never rewritten. Task 2's merge is what first exercises the write path, and it
  does so against astrograph.

Task 2: NOT STARTED — blocked on Task 1 merge + CI apply.
