"""Small podman helpers shared by the bench/lib scripts (no third-party deps)."""
import json
import os
import subprocess


def run(cmd, timeout=120, env_extra=None, input_text=None):
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, input=input_text)


def project_containers(project, running_only=True):
    cmd = ["podman", "ps", "--filter", f"label=com.docker.compose.project={project}",
           "--format", '{{.Names}}\t{{index .Labels "com.docker.compose.service"}}']
    if not running_only:
        cmd.insert(2, "-a")
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"podman ps failed: {r.stderr.strip()[:200]}")
    out = []
    for line in r.stdout.splitlines():
        if line.strip():
            name, _, svc = line.partition("\t")
            out.append((name, svc))
    return out


def container_for(project, service):
    for name, svc in project_containers(project, running_only=False):
        if svc == service:
            return name
    raise RuntimeError(f"no container for service '{service}' in project '{project}'")


def inspect(name):
    r = run(["podman", "inspect", name])
    if r.returncode != 0:
        raise RuntimeError(f"podman inspect {name}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)[0]


def proc_cgroup(pid):
    """cgroup v2 path of a pid ('0::/path')."""
    with open(f"/proc/{pid}/cgroup") as fh:
        for line in fh:
            if line.startswith("0::"):
                return line.strip()[3:]
    raise RuntimeError(f"no cgroup v2 entry for pid {pid}")


def descendants(root_pid):
    """root_pid and all its descendants, from /proc (container pids are host pids)."""
    children = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
            children.setdefault(ppid, []).append(int(d))
        except (OSError, IndexError, ValueError):
            continue
    out, stack = [], [int(root_pid)]
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(children.get(p, []))
    return out
