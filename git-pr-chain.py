#!/usr/bin/env python3

# Requires
#  pip3 install pyyaml
#  pip3 install PyGithub

import argparse
import collections
import concurrent.futures
import datetime
import functools
import inspect
import itertools
import json
import json
import os
import re
import subprocess
import sys
import textwrap
import yaml
from typing import List, Dict, Tuple, Iterable, Optional

# Not compatible with pytype; ignore using instructions from
# https://github.com/google/pytype/issues/80
from github import Github  # type: ignore

VERBOSE = False
DRY_RUN = False


def fatal(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr)


def traced(fn, show_start=False, show_end=True):
    """Decorator that "traces" a function under --verbose.

    - If VERBOSE and show_start are true, prints a message when the function
      starts.
    - If VERBOSE show_end are true, prints a message when the function
      ends.
    """

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        starttime = datetime.datetime.now()
        if VERBOSE and show_start:
            print(f"{fn.__name__}({args}, {kwargs}) starting")
        ret = fn(*args, **kwargs)
        if VERBOSE and show_end:
            one_ms = datetime.timedelta(milliseconds=1)
            ms = (datetime.datetime.now() - starttime) / one_ms
            print(f"{fn.__name__}({args}, {kwargs}) returned in {ms:.0f}ms: {ret}")
        return ret

    return inner


@functools.lru_cache()
def gh_client():
    # Try to get the github oauth token out of gh or hub's config file.  Yes, I
    # am that evil.
    config_dir = os.environ.get(
        "XDG_CONFIG_HOME", os.path.join(os.environ["HOME"], ".config")
    )

    def get_token_from(fname):
        try:
            with open(os.path.join(config_dir, fname)) as f:
                config = yaml.safe_load(f)
                return config["github.com"][0]["oauth_token"]
        except (FileNotFoundError, KeyError, IndexError):
            return None

    token = None
    for fname in ["gh/config.yml", "hub"]:
        token = get_token_from(fname)
        if token:
            break
    if not token:
        fatal(
            "Couldn't get oauth token from gh or hub.  Install one of those "
            "tools and authenticate to github."
        )
    return Github(token)


@functools.lru_cache()
@traced
def gh_repo_client():
    remote = git_upstream_remote()

    # Translate our remote's URL into a github user/repo string.  (Is there
    # seriously not a beter way to do this?)
    remote_url = git("remote", "get-url", remote)
    match = re.search(r"(?:[/:])([^/:]+/[^/:]+)\.git$", remote_url)
    if not match:
        fatal(
            f"Couldn't extract github user/repo from {remote} "
            f"remote URL {remote_url}."
        )
    gh_repo_name = match.group(1)
    return gh_client().get_repo(gh_repo_name)


class cached_property:
    """
    (Bad) backport of python3.8's functools.cached_property.
    """

    def __init__(self, fn):
        self.__doc__ = fn.__doc__
        self.fn = fn

    def __get__(self, instance, cls):
        if instance is None:
            return self
        val = self.fn(instance)
        instance.__dict__[self.fn.__name__] = val
        return val


@traced
def git(*args, err_ok=False, stderr_to_stdout=False):
    """Runs a git command, returning the output."""
    stderr = subprocess.DEVNULL if err_ok else None
    stderr = subprocess.STDOUT if stderr_to_stdout else stderr
    try:
        return (
            subprocess.check_output(["git"] + list(args), stderr=stderr)
            .decode("utf-8")
            .strip()
        )
    except subprocess.CalledProcessError:
        if not err_ok:
            raise
        return ""


class Commit:
    # TODO: We could compute some/all of these properties with a single call to
    # git, rather than one for each.  Indeed, we could do it with a single call
    # to git for *all* of the commits we're interested in, all at once.  Does
    # it matter?  Maybe not, network ops are so much slower.

    def __init__(self, sha: str, parent: Optional["Commit"]):
        self.sha = sha
        self.parent = parent

    @cached_property
    @traced
    def gh_branch(self):
        """Branch that contains this commit in github."""
        if self.not_to_be_pushed:
            return None

        # Search the commit message for 'git-pr-chain: XYZ' or 'GPC: XYZ'.
        matches = re.findall(
            r"^(?:git-pr-chain|GPC):\s*(.*)", self.commit_msg, re.MULTILINE
        )
        if not matches:
            return self.parent.gh_branch if self.parent else None
        if len(matches) == 1:
            # findall returns the groups directly, so matches[0] is group 1 of
            # the match -- which is what we want.
            return matches[0]
        fatal(
            f"Commit {self.sha} has multiple git-pr-chain lines.  Rewrite "
            "history and fix this."
        )

    @cached_property
    @traced
    def not_to_be_pushed(self) -> bool:
        if self.parent and self.parent.not_to_be_pushed:
            return True

        # Search the commit message for 'git-pr-chain: STOP' or 'GPC: STOP'.
        return bool(re.search(r"(git-pr-chain|GPC):\s*STOP\>", self.commit_msg))

    @cached_property
    @traced
    def is_merge_commit(self):
        return len(git("show", "--no-patch", "--format=%P", self.sha).split("\n")) > 1

    @cached_property
    @traced
    def shortdesc(self):
        shortsha = git("rev-parse", "--short", self.sha)
        shortmsg = git("show", "--no-patch", "--format=%s", self.sha)
        return f"{shortsha} {shortmsg}"

    @cached_property
    @traced
    def commit_msg(self):
        return git("show", "--no-patch", "--format=%B", self.sha)


@functools.lru_cache()
@traced
def git_upstream_branch(branch=None):
    """Gets the upstream branch tracked by `branch`.

    If `branch` is none, uses the current branch.
    """
    if not branch:
        branch = "HEAD"
    branchref = git("rev-parse", "--symbolic-full-name", branch)
    return git("for-each-ref", "--format=%(upstream:short)", branchref)


def git_upstream_remote(branch=None):
    # Get the name of the upstream remote (e.g. "origin") that this branch is
    # downstream from.
    return git_upstream_branch(branch).split("/")[0]


@functools.lru_cache()
@traced
def gh_branch_prefix():
    return git("config", "pr-chain.branch-prefix").strip()


def validate_branch_commits(commits: Iterable[Commit]) -> None:
    def list_strs(strs):
        return "\n".join("  - " + s for s in strs)

    def list_commits(cs):
        return list_strs(c.shortdesc for c in cs)

    merge_commits = [c for c in commits if c.is_merge_commit]
    if merge_commits:
        fatal(
            textwrap.dedent(
                f"""\
                History contained merge commit(s):

                {list_commits(merge_commits)}

                Merges are incompatible with git-pr-chain.  Rewrite your branch
                to a linear history."""
            )
        )

    grouped_commits = itertools.groupby(commits, lambda c: c.gh_branch)

    # Count the number of times each github branch appears in grouped_commits.
    # If a branch appears more than once, that means we have an "AABA"
    # situation, where we go to one branch, then to another, then back to the
    # first.  That's not allowed.
    #
    # Ignore the `None` branch here.  It's allowed to appear at the beginning
    # and end of the list (but nowhere else!), and that invariant is checked
    # below.
    ctr = collections.Counter(branch for branch, _ in grouped_commits if branch)
    repeated_branches = [branch for branch, count in ctr.items() if count > 1]
    if repeated_branches:
        fatal(
            textwrap.dedent(
                f"""\
                Upstream branch(es) AABA problem.  The following upstream
                branches appear, then are interrupted by a different branch,
                then reappear.

                {list_strs(repeated_branches)}

                This is not allowed; reorder commits or change their upstream
                branches."""
            )
        )

    # Check for github_branch == None; this is disallowed except for the first
    # and last group.  I don't think users can get into this situation
    # themselves.
    commits_without_branch = list(
        itertools.chain.from_iterable(
            cs for branch, cs in list(grouped_commits)[1:-1] if not branch
        )
    )
    if commits_without_branch:
        fatal(
            textwrap.dedent(
                f"""\
              Unable to infer upstream branches for commit(s):

              {list_commits(commits_without_branch)}

              This shouldn't happen and is probably a bug in git-pr-chain."""
            )
        )


@traced
def branch_commits() -> List[Commit]:
    """Get the commits in this branch.

    The first commit is the one connected to the branch base.  The last commit
    is HEAD.
    """
    upstream_branch = git_upstream_branch()
    if not upstream_branch:
        fatal(
            "Set an upstream branch with e.g. `git branch "
            "--set-upstream-to origin/master`"
        )
    commits = []
    for sha in (
        git("log", "--reverse", "--pretty=%H", f"{upstream_branch}..HEAD")
        .strip()
        .split("\n")
    ):
        # Filter out empty commits (if e.g. the git log produces nothing).
        if not sha:
            continue
        parent = commits[-1] if commits else None
        commits.append(Commit(sha, parent=parent))

    # Infer upstream branch names on commits that don't have one explicitly in
    # the commit message
    for idx, c in enumerate(commits):
        if idx == 0:
            continue
        if not c.gh_branch:
            c.inferred_upstream_branch = commits[idx - 1].gh_branch

    validate_branch_commits(commits)
    return commits


def cmd_show(args):
    commits = branch_commits()
    if not commits:
        fatal(
            "No commits in branch.  Is the upstream branch (git branch "
            "--set-upstream-to <origin/master or something> set correctly?"
        )

    print(
        f"Current branch is downstream from {git_upstream_branch()}, "
        f"{len(commits)} commit(s) ahead.\n"
    )
    for branch, cs in itertools.groupby(commits, lambda c: c.gh_branch):
        cs = list(cs)

        # TODO Link to PR if it exists.
        # TODO Link to branch on github
        if branch:
            print(f"Github branch {branch}")
        else:
            first = cs[0]
            if first.not_to_be_pushed:
                print("Will not be pushed; remove git-pr-chain:STOP to push.")
            else:
                print(
                    f"No github branch; will not be pushed. "
                    '(Add "git-pr-chain: <branch>" to commit msg.)'
                )
        for c in cs:
            # Indent two spaces, then call git log directly, so we get nicely
            # colorized output.
            print("  ", end="")
            sys.stdout.flush()
            subprocess.run(("git", "--no-pager", "log", "-n1", "--oneline", c.sha))


def push_branches(args):
    commits = branch_commits()
    grouped_commits = [
        (gh_branch_prefix() + branch, list(cs))
        for branch, cs in itertools.groupby(commits, lambda c: c.gh_branch)
    ]
    remote = git_upstream_remote()

    def push(branch_and_commits):
        branch, cs = branch_and_commits
        if not DRY_RUN:
            print(
                f"Pushing {branch} to {remote}:\n"
                + git("push", "-f", remote, f"{cs[-1].sha}:refs/heads/{branch}", stderr_to_stdout=True)
                + '\n'
            )
        else:
            print(f"DRY RUN: Pushing {branch} to {remote}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
        executor.map(push, grouped_commits)


def create_and_update_prs(args):
    commits = branch_commits()
    grouped_commits = [
        (gh_branch_prefix() + branch, list(cs))
        for branch, cs in itertools.groupby(commits, lambda c: c.gh_branch)
    ]

    # Create or update PRs for each branch.
    repo = gh_repo_client()
    for idx, (branch, cs) in enumerate(grouped_commits):
        if idx > 0:
            base, _ = grouped_commits[idx - 1]
        else:
            base = git_upstream_branch().split("/")[-1]

        # For some reason, the `head=branch` filter doesn't seem to work!
        # Perhaps we should just pull all PRs once, at the beginning?
        prs = [
            pr
            for pr in repo.get_pulls(state="open", head=branch)
            if pr.head.ref == branch
        ]
        if not prs:
            if VERBOSE or DRY_RUN:
                print(f"Creating PR for {branch}, base {base}...")
            # TODO: Put the PR stack in the body.
            # TODO: Open an editor for title and body?  (Do both at once.)
            # TODO: Verify that everything is OK before pushing anything?
            if not DRY_RUN:
                pr = repo.create_pull(title=branch, body="", base=base, head=branch)
                # TODO: Auto-open this URL.
                print(f"Created {pr.html_url}")
        elif len(prs) == 1:
            pr = prs[0]
            if pr.base.ref != base:
                if VERBOSE or DRY_RUN:
                    print(
                        f"Updating base branch for {branch} (PR #{pr.number}) "
                        f"from {pr.base.ref} to {base}"
                    )
                if not DRY_RUN:
                    pr.edit(base=base)
        else:
            joined_pr_urls = "\n".join(pr.url for pr in prs)
            fatal(
                textwrap.dedent(
                    f"""\
                    Branch {branch} has multiple open PRs:
                    {joined_pr_urls}
                    Don't know which to choose!"""
                )
            )


def cmd_upload(args):
    push_branches(args)
    create_and_update_prs(args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true")
    subparser = parser.add_subparsers()

    sp_show = subparser.add_parser("show", help="List commits in chain")
    sp_show.set_defaults(func=cmd_show)

    sp_upload = subparser.add_parser("upload", help="Create and update PRs in github")
    sp_upload.set_defaults(func=cmd_upload)

    def cmd_help(args):
        if "command" not in args or not args.command:
            parser.print_help()
        elif args.command == "show":
            sp_show.print_help()
        elif args.command == "upload":
            sp_upload.print_help()
        elif args.command == "help":
            print("Well aren't you trying to be clever.", file=sys.stderr)
        else:
            print(f"Unrecognized subcommand {args.command}", file=sys.stderr)
            parser.print_help()
            sys.exit(1)

    sp_help = subparser.add_parser("help")
    sp_help.add_argument("command", nargs="?", help="Subcommand to get help on")
    sp_help.set_defaults(func=cmd_help)

    args = parser.parse_args()
    if "func" not in args:
        print("Specify a subcommand!")
        parser.print_help()
        sys.exit(1)

    global VERBOSE
    VERBOSE = args.verbose

    global DRY_RUN
    DRY_RUN = args.dry_run

    args.func(args)


if __name__ == "__main__":
    main()
