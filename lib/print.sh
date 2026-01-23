#!/bin/bash
# lib/print.sh - Output helpers
# Zero dependencies, safe everywhere

print_header() { echo "=== $* ==="; }
print_action() { echo "==> $*"; }
print_sub()    { echo "    $*"; }
print_ok()     { echo "    ✓ $*"; }
print_warn()   { echo "    ✗ Warning: $*"; }
