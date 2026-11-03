require '_h2ph_pre.ph';

no warnings qw(redefine misc);

if(!defined (&_SYS_WAIT_H)  && !defined (&_STDLIB_H)) {
    die("Never include <bits/waitstatus.h> directly; use <sys/wait.h> instead.");
}
eval 'sub __WEXITSTATUS {
    my($status) = @_;
	eval q(((($status) & 0xff00) >> 8));
}' unless defined(&__WEXITSTATUS);
eval 'sub __WTERMSIG {
    my($status) = @_;
	eval q((($status) & 0x7f));
}' unless defined(&__WTERMSIG);
eval 'sub __WSTOPSIG {
    my($status) = @_;
	eval q( &__WEXITSTATUS($status));
}' unless defined(&__WSTOPSIG);
eval 'sub __WIFEXITED {
    my($status) = @_;
	eval q(( &__WTERMSIG($status) == 0));
}' unless defined(&__WIFEXITED);
eval 'sub __WIFSIGNALED {
    my($status) = @_;
	eval q((( ((($status) & 0x7f) + 1) >> 1) > 0));
}' unless defined(&__WIFSIGNALED);
eval 'sub __WIFSTOPPED {
    my($status) = @_;
	eval q(((($status) & 0xff) == 0x7f));
}' unless defined(&__WIFSTOPPED);
if(defined(&WCONTINUED)) {
    eval 'sub __WIFCONTINUED {
        my($status) = @_;
	    eval q((($status) ==  &__W_CONTINUED));
    }' unless defined(&__WIFCONTINUED);
}
eval 'sub __WCOREDUMP {
    my($status) = @_;
	eval q((($status) &  &__WCOREFLAG));
}' unless defined(&__WCOREDUMP);
eval 'sub __W_EXITCODE {
    my($ret, $sig) = @_;
	eval q((($ret) << 8| ($sig)));
}' unless defined(&__W_EXITCODE);
eval 'sub __W_STOPCODE {
    my($sig) = @_;
	eval q((($sig) << 8| 0x7f));
}' unless defined(&__W_STOPCODE);
eval 'sub __W_CONTINUED () {0xffff;}' unless defined(&__W_CONTINUED);
eval 'sub __WCOREFLAG () {0x80;}' unless defined(&__WCOREFLAG);
1;
