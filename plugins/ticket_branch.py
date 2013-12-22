# -*- coding: utf-8 -*-

from genshi.builder import tag
from genshi.filters import Transformer

from trac.core import *
from trac.web.api import ITemplateStreamFilter
from trac.ticket.api import ITicketManipulator

import subprocess
import os.path
import re
import urllib

branch_re = re.compile(r"^(?!.*/\.)(?!.*\.\.)(?!/)(?!.*//)(?!.*@\{)(?!.*\\)[^\040\177 ~^:?*[]+(?<!\.lock)(?<!/)(?<!\.)$") # http://stackoverflow.com/questions/12093748/how-do-i-check-for-valid-git-branch-names

change_re = re.compile('^\+=======$', re.MULTILINE)
removed_re = re.compile('removed in (?:local|remote)\n  (?:base|our|their)   \d{6} ([a-f\d]{40}) .+\n  (?:base|our|their)  \d{6} ([a-f\d]{40})')

MASTER_BRANCH = u'develop'
MAX_NEW_COMMITS = 10

GIT_RANGE_LOG_URL = 'http://git.sagemath.org/sage.git/log/?h={branch}&qt=range&q={base}..{branch}'
GIT_RANGE_LOG_URL = GIT_RANGE_LOG_URL.format(base=urllib.quote(MASTER_BRANCH, ''), branch='{branch}')

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
    implements(ITicketManipulator)

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

        branch = branch_name = branch.strip()

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
            base = self._merge_base(master, branch)
        except GitError:
            return error("failed to determine common ancestor")

        if base == branch:
            return error("no commits on branch yet")

        filters = [FILTER.append(tag.a('(Commits)',
                href=GIT_RANGE_LOG_URL.format(branch=urllib.quote(branch_name, ''))))]

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

    def _merge_base(self, tree1, tree2):
        try:
            return self.__git('merge-base', tree1, tree2).strip()
        except subprocess.CalledProcessError as e:
            self.log.error("%s failed with exit code %s. The output was:\n%s"%(e.cmd,e.returncode,e.output))
            raise GitError("could not determine merge base of `%s` and `%s`"%(tree1, tree2))

    def _valid_commit(self, val):
        if not isinstance(val, basestring):
            return
        if len(val) != 40:
            return
        try:
            int(val, 16)
            return val.lower()
        except ValueError:
            return

    def get_commit_link(self, sha1):
        return 'http://git.sagemath.org/sage.git/commit/?id={0}'.format(sha1)

    def log_table(self, new_commit, limit=None, ignore=[]):
        git_cmd = ['log', '--oneline']
        if limit is not None:
            git_cmd.append('--max-count={0}'.format(limit))
        git_cmd.append(new_commit)
        for branch in ignore:
            git_cmd.append('^{0}'.format(branch))
        log = self.__git(*git_cmd)
        table = []
        for line in log.splitlines():
            short_sha1 = line[:7]
            title = line[8:].decode('utf8')
            table.append(u'||[[%s|%s]]||{{{%s}}}||'%(self.get_commit_link(short_sha1), short_sha1, title))
        return table

    # doesn't actually do anything, according to the api
    def prepare_ticket(self, req, ticket, fields, actions): pass

    # hack changes into validate_ticket, since api is currently stilly
    def validate_ticket(self, req, ticket):
        branch = ticket['branch']
        old_commit = self._valid_commit(ticket['commit'])
        if branch:
            ticket['branch'] = branch = branch.strip()
            try:
                commit = ticket['commit'] = unicode(self._dereference_head(branch))
            except GitError:
                commit = ticket['commit'] = u''
        else:
            commit = ticket['commit'] = u''

        if (req.args.get('preview') is None and
                req.args.get('comment') is not None and
                commit and
                commit != old_commit):
            ignore = {MASTER_BRANCH}
            if old_commit is not None:
                ignore.add(old_commit)
            try:
                table = self.log_table(commit, ignore=ignore)
            except GitError:
                return []
            if len(table) > MAX_NEW_COMMITS:
                header = u'Last {0} new commits:'.format(MAX_NEW_COMMITS)
                table = table[:MAX_NEW_COMMITS]
            else:
                header = u'New commits:'
            if table:
                comment = req.args['comment'].splitlines()
                if comment:
                    comment.append(u'----')
                comment.append(header)
                comment.extend(table)
                req.args['comment'] = u'\n'.join(comment)

        return []
