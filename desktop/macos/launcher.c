// The executable of Bentobox.app. It runs the command in its arguments (the Bentobox app, with the project's Python) as a
// child process and waits for it. macOS then counts the child's Bluetooth use as this app's, so it asks once for
// "Bentobox", even when launchd starts it with no terminal.
// Build: desktop/install.py app (or see that function).
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child;

static void forward(int sig)
{
    if (child > 0) kill(child, sig);
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s COMMAND [ARGS...]\n", argv[0]);
        return 64;
    }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    if (posix_spawn(&child, argv[1], NULL, NULL, &argv[1], environ) != 0) {
        perror(argv[1]);
        return 127;
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) { // only a signal may interrupt the wait; anything else and there's no child to wait for
            perror("waitpid");
            return 70;
        }
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
