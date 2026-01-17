#!/bin/bash
rsync -avz --chown=99:100 -e "ssh -i /root/.ssh/private" root@10.0.40.100:/etc/kvmd/override.yaml /mnt/cache/tower/pi-kvm/
rsync -avz --chown=99:100 -e "ssh -i /root/.ssh/private" root@10.0.40.100:/etc/kvmd/meta.yaml /mnt/cache/tower/pi-kvm/
rsync -avz --chown=99:100 -e "ssh -i /root/.ssh/private" root@10.0.40.100:/etc/pacman.conf /mnt/cache/tower/pi-kvm/