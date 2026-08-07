#! /bin/bash

set -eo pipefail

# save the current user and group IDs
new_uid=$1
new_gid=$2

# save the current sandbox user and group IDs
old_uid=$(id -u sandbox)
old_gid=$(id -g sandbox)

# change the sandbox user and group IDs to match the current user and group IDs
/usr/sbin/usermod -u "$new_uid" sandbox
/usr/sbin/groupmod -g "$new_gid" sandbox

# change the ownership of all files on the filesystem from the old sandbox user and group IDs to the new user and group IDs
find / -xdev -user "$old_uid" -exec chown -h "$new_uid" {} \; 2>/dev/null
find / -xdev -group "$old_gid" -exec chgrp -h "$new_gid" {} \; 2>/dev/null
