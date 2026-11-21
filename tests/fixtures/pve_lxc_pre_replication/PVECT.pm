package PVE::HA::Resources::PVECT;

# Trimmed from pve-ha-manager 5.2.6 (AGPL-3.0, Proxmox Server Solutions GmbH):
# PVE::HA::Resources::PVECT::migrate, verbatim.

use strict;
use warnings;

sub migrate {
    my ($class, $haenv, $id, $target, $online, $migrate_options) = @_;

    my $nodename = $haenv->nodename();

    my $params = {
        node => $nodename,
        vmid => $id,
        target => $target,
        online => 0, # we cannot migrate CT (yet) online, only relocate
    };

    $migrate_options //= {};
    $params->{$_} = $migrate_options->{$_} for keys $migrate_options->%*;

    # always relocate container for now
    if ($class->check_running($haenv, $id)) {
        $class->shutdown($haenv, $id);
    }

    my $oldconfig = $class->config_file($id, $nodename);

    my $upid = PVE::API2::LXC->migrate_vm($params);
    PVE::HA::Tools::upid_wait($upid, $haenv);

    # check if vm really moved
    return !(-f $oldconfig);
}

1;
