package PVE::LXC::Migrate;

# Trimmed from pve-container 6.1.24 (AGPL-3.0, Proxmox Server Solutions GmbH):
# the start and end of PVE::LXC::Migrate::prepare, verbatim, with the volume
# and remote-tunnel checks in between cut out.

use strict;
use warnings;

sub prepare {
    my ($self, $vmid) = @_;

    my $online = $self->{opts}->{online};
    my $restart = $self->{opts}->{restart};
    my $remote = $self->{opts}->{remote};

    my $running = 0;
    if (PVE::LXC::check_running($vmid)) {
        die "lxc live migration is currently not implemented\n" if $online;
        die "running container can only be migrated in restart mode" if !$restart;
        $running = 1;
    }
    $self->{was_running} = $running;

    if ($remote) {
        $self->log('info', "websocket tunnel started\n");
    } else {
        $self->prepare_local_cluster_checks();
    }

    # in restart mode, we shutdown the container before migrating
    if ($restart && $running) {
        my $timeout = $self->{opts}->{timeout} // 180;

        $self->log('info', "shutdown CT $vmid\n");

        eval { PVE::LXC::vm_stop($vmid, 0, $timeout) };
        if (my $err = $@) {
            # the target of a remote migration already reserved the VMID with a locked config
            if (my $tunnel = delete $self->{tunnel}) {
                eval { PVE::Tunnel::finish_tunnel($tunnel, 1); };
                $self->log('warn', "cleanup of the target failed - $@") if $@;
            }
            die $err;
        }

        $running = 0;
    }

    return $running;
}

1;
