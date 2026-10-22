"""`./roll`: the read-only half of the rolling PVE reboot.

`./roll plan` surveys every PVE node and every own-kernel VM, applies the
runbook's stop conditions, and prints the wave order. `./roll verify <host>...`
checks hosts after their reboot. Neither reboots anything; `/roll` asks
the human once and the operator issues each reboot.

    parse    - survey output to values (pure)
    facts    - one host's sections to typed facts (pure)
    checks   - pre-flight and recovery findings (pure)
    plan     - wave order, the plan and its rendering (pure)
    windows  - Alertmanager maintenance windows, read from monitoring-config
    survey   - inventory, SSH reads, and the two commands
"""
