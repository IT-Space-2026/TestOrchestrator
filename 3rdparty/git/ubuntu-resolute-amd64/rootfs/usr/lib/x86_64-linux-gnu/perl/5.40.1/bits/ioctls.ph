require '_h2ph_pre.ph';

no warnings qw(redefine misc);

unless(defined(&_SYS_IOCTL_H)) {
    die("Never use <bits/ioctls.h> directly; include <sys/ioctl.h> instead.");
}
require 'asm/ioctls.ph';
require 'linux/sockios.ph';
1;
