# -*- coding: utf-8 -*-

from genshi.builder import tag
from genshi.filters import Transformer

from trac.core import *
from trac.web.api import ITemplateStreamFilter
from trac.ticket.api import ITicketChangeListener

import subprocess
import os.path
import re

branch_re = re.compile(r"^(?!.*/\.)(?!.*\.\.)(?!/)(?!.*//)(?!.*@\{)(?!.*\\)[^\040\177 ~^:?*[]+(?<!\.lock)(?<!/)(?<!\.)$") # http://stackoverflow.com/questions/12093748/how-do-i-check-for-valid-git-branch-names

change_re = re.compile('^\+=======$', re.MULTILINE)
removed_re = re.compile('removed in (?:local|remote)\n  (?:base|our|their)   \d{6} ([a-f\d]{40}) .+\n  (?:base|our|their)  \d{6} ([a-f\d]{40})')

MASTER_BRANCH = 'u/ohanar/build_system'

def _is_clean_merge(merge_tree):
    for match in change_re.finditer(merge_tree):
        return False

    for match in removed_re.finditer(merge_tree):
        sha1, sha2 = match.groups()
        if sha1 != sha2:
            return False

    return True

class GitError(Exception):
    pass

FILTER = Transformer('//td[@headers="h_branch"]')
FILTER_TEXT = Transformer('//td[@headers="h_branch"]/text()')

class TicketBranch(Component):
    """
    A Sage specific plugin which formats the ``branch`` field of a ticket and
    applies changes to the ``branch`` field to the git repository.
    """
    implements(ITemplateStreamFilter)
    implements(ITicketChangeListener)

    def __init__(self, *args, **kwargs):
        Component.__init__(self, *args, **kwargs)
        self.git_dir = self.config.get("trac","repository_dir","")
        if not self.git_dir:
            raise TracError("[trac] repository_dir is not set in the config file")
        self._master = None
        self._cache = {}
        self._tree_cache = {}

    def filter_stream(self, req, method, filename, stream, data):
        """
        Reformat the ``branch`` field of a ticket to show the history of the
        linked branch.
        """
        branch = data.get('ticket', {'branch':None})['branch']
        if filename != 'ticket.html' or not branch:
            return stream

        def error_filters(error):
            return FILTER.attr("class", "needs_work"), FILTER.attr("title", error)

        def apply_filters(filters):
            s = stream
            for filter in filters:
                s |= filter
            return s

        def error(error, filters=()):
            filters = tuple(filters)+error_filters(error)
            return apply_filters(filters)


        if not self._is_valid_branch_name(branch):
            return error("not a valid branch name")
        if not self._is_existing_branch(branch):
            return error("branch does not exist")

        master = self._dereference_head(MASTER_BRANCH)
        if master != self._master:
            self._master = master
            self._cache = {}
            self._tree_cache = {}

        branch = self._dereference_head(branch)

        if branch in self._cache:
            return apply_filters(self._cache[branch])

        def apply_filters(filters):
            self._cache[branch] = filters
            s = stream
            for filter in filters:
                s |= filter
            return s

        try:
            common_ancestor = self._common_ancestor(master, branch)
        except GitError:
            return error("failed to determine common ancestor")

        if common_ancestor == branch:
            return error("no commits on branch yet")

        filters = [FILTER.append(tag.a('(Commits)',
                href=req.href.log(rev=branch,stop_rev=common_ancestor)))]

        base = self.__git('merge-base', master, branch).strip()
        merge_tree = self.__git('merge-tree', base, master, branch)
        if _is_clean_merge(merge_tree):
            self._tree_cache[branch] = merge_tree
        else:
            return error("does not merge cleanly", filters)

        filters.append(FILTER_TEXT.wrap(tag.a(class_="positive_review",
                href=req.href.changeset(base=base, old=master, new=branch))))

        self._tree_cache[branch] = merge_tree

        return apply_filters(filters)

    def _is_valid_branch_name(self, branch):
        """
        Returns whether ``branch`` is a valid git branch name.
        """
        return bool(branch_re.match(branch))

    def _is_existing_branch(self, branch):
        """
        Returns whether ``branch`` is a valid git branch that exists in the git
        repository.
        """
        try:
            return \
                self._is_valid_branch_name(branch) and \
                bool(self.__git("branch","--list",branch).strip()) # the branch exists if there is something in the output
        except subprocess.CalledProcessError as e:
            self.log.debug("%s failed with exit code %s. The output was:\n%s"%(e.cmd,e.returncode,e.output))
            return False

    def _is_existing_head(self, head):
        """
        Returns whether ``head`` is a valid git head that exists in the git
        repository.
        """
        try:
            self._dereference_head(head)
        except GitError:
            return False

        return True

    def _common_ancestor(self, a, b):
        """
        Return the common ancestor of the commits a and b.
        """
        try:
            rev_a = self.__git('rev-list', '--first-parent', a).splitlines()
            rev_b = self.__git('rev-list', '--first-parent', b).splitlines()
            while rev_a and rev_b:
                sha1_a = rev_a.pop()
                sha1_b = rev_b.pop()
                if sha1_a == sha1_b:
                    ret = sha1_a
                else:
                    return ret
            return ret
        except subprocess.CalledProcessError as e:
            self.log.error("%s failed with exit code %s. The output was:\n%s"%(e.cmd,e.returncode,e.output))
            raise GitError("no common ancestor")
        except NameError:
            raise GitError("no common ancestor")

    def _dereference_head(self, head):
        """
        Returns the SHA1 which (the existing) ``head`` points to.
        """
        try:
            return self.__git("show-ref","--heads","-s",head).split('\n')[0]
        except subprocess.CalledProcessError as e:
            self.log.error("%s failed with exit code %s. The output was:\n%s"%(e.cmd,e.returncode,e.output))
            raise GitError("could not dereference `%s`"%head)
        except IndexError:
            raise GitError("could not dereference `%s`"%head)

    def __git(self, *args):
        """
        Helper to run a git command.
        """
        return subprocess.check_output(["git","--git-dir=%s"%self.git_dir]+list(args))

    def ticket_created(self, ticket): pass

    def ticket_deleted(self, ticket): pass

    def ticket_changed(self, ticket, author, comment, old_values):
        """
        If the ``branch`` field of a ticket changes, the ``t/ticket_number``
        should reflect this change:

        - If ``branch`` is now empty or if ``branch`` is invalid, then the
          symbolic ref ``t/ticket_number`` is deleted.

        - If ``branch`` changes, then the symbolic ref ``t/ticket_number``
          points to the new branch.

        """
        # need to figure out a way to do this using git tools, so currently we disable
        return

        if 'branch' not in old_values:
            # branch has not changed
            return

        ticket_ref = "refs/heads/t/%s"%ticket.id
        old_branch = old_values['branch']
        if old_branch is None:
            old_branch = ""
        old_branch = old_branch.strip()
        new_branch = ticket['branch'].strip()

        if new_branch and self._is_existing_branch(new_branch):
            try:
                subprocess.check_output(["ln","-sf",new_branch,os.path.join(self.git_dir,ticket_ref)])
            except subprocess.CalledProcessError as e:
                self.log.error("%s failed with exit code %s. The output was:\n%s"%(e.cmd,e.returncode,e.output))
        else:
            try:
                subprocess.check_output(["rm","-f",os.path.join(self.git_dir,ticket_ref)])
            except subprocess.CalledProcessError as e:
                self.log.error("%s failed with exit code %s. The output was:\n%s"%(e.cmd,e.returncode,e.output))
