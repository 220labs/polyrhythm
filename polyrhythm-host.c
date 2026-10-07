/*
 * polyrhythm-host — the program launchd starts for polyrhythm (dev.220labs.polyrhythm).
 *
 * Why a compiled launcher instead of python3 in the plist: macOS grants Full Disk Access (to
 * read ~/Library/Messages/chat.db) and Automation -> Messages (to send) to the *responsible
 * process* — for a launchd job, the program the job starts. Granting them to /usr/bin/python3
 * would hand both to every Python script on the Mac. This binary is the one thing the grants
 * name, and it does one thing: `polyrhythm.py run`, for the polyrhythm.py whose path was
 * compiled into it.
 *
 * Nothing from whoever starts it steers it: the script path is compiled in (install.sh:
 * -DSCRIPT=…), it takes no arguments (an argument such as `scan --reply '<shell>'` would run
 * anything with its grants), and it drops the environment variables that would choose what
 * runs — POLYRHYTHM_* (they override .env), DEVELOPER_DIR, SDKROOT and TOOLCHAINS (the xcrun
 * shim at /usr/bin/python3 honours them), PYTHON* and DYLD_*. Python runs isolated (-I): no
 * PYTHONPATH, no user site-packages, no module shadowing from the script's folder. What it
 * cannot stop: anything that can write to that folder can change polyrhythm.py itself, and the
 * `claude` the job finds on its PATH runs with the grants too (README, "Security model").
 *
 * It must SPAWN python as a child, never exec it: exec replaces the image and the process
 * would present as python to TCC; a child inherits its parent's responsibility.
 *
 * Built and ad-hoc signed by install.sh into ~/.local/bin/polyrhythm-host. TCC keys an ad-hoc
 * signature on its code hash, so a rebuild needs the two grants again — install.sh rebuilds
 * only when this file or the script's path changed, and says so.
 *
 * Usage: polyrhythm-host   (no arguments; launchd starts it)
 */
#include <errno.h>
#include <libgen.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef SCRIPT
#error "build with -DSCRIPT='\"/absolute/path/to/polyrhythm.py\"' (install.sh does)"
#endif

extern char **environ;
static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static int steers(const char *kv) {
    static const char *const prefixes[] = {"POLYRHYTHM_", "DEVELOPER_DIR=", "SDKROOT=",
                                           "TOOLCHAINS=", "PYTHON", "DYLD_"};
    for (size_t i = 0; i < sizeof prefixes / sizeof *prefixes; i++)
        if (strncmp(kv, prefixes[i], strlen(prefixes[i])) == 0) return 1;
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 1) {
        fprintf(stderr, "usage: %s (no arguments: it runs %s run)\n", argv[0], SCRIPT);
        return 2;
    }
    char dir[4096];
    snprintf(dir, sizeof dir, "%s", SCRIPT);
    char *args[] = {"/usr/bin/python3", "-I", "-u", SCRIPT, "run", NULL};

    size_t n = 0;
    while (environ[n]) n++;
    char **env = calloc(n + 1, sizeof(char *));
    if (!env) { perror("calloc"); return 1; }
    size_t k = 0;
    for (size_t i = 0; i < n; i++)
        if (!steers(environ[i])) env[k++] = environ[i];

    if (chdir(dirname(dir)) != 0) { perror("chdir"); return 1; }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    int rc = posix_spawn(&child, args[0], NULL, NULL, args, env);
    if (rc != 0) { fprintf(stderr, "spawn: %s\n", strerror(rc)); return 1; }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) { perror("waitpid"); return 1; }
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
