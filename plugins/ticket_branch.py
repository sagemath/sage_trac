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

import pygit2

MASTER_BRANCH = u'develop'
MAX_NEW_COMMITS = 10

GIT_BASE_URL      = 'http://git.sagemath.org/sage.git/'
GIT_LOG_RANGE_URL = GIT_BASE_URL + 'log/?h={branch}&qt=range&q={base}..{branch}'
GIT_DIFF_URL    = GIT_BASE_URL + 'diff/?id={commit}'
GIT_DIFF_RANGE_URL    = GIT_BASE_URL + 'diff/?id2={base}&id={branch}'

TRAC_SIGNATURE = pygit2.Signature('trac', 'trac@sagemath.org')

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

        branch = branch_name = branch.strip()

        branch = self._git.lookup_branch(branch)
        if branch is None:
            return error("branch does not exist")
        else:
            branch = branch.get_object()

        filters = [FILTER.append(tag.a('(Commits)',
                href=GIT_LOG_RANGE_URL.format(
                    base=urllib.quote(MASTER_BRANCH, ''),
                    branch=urllib.quote(branch_name, ''))
                ))]

        tmp = self._get_cache(branch)
        if tmp is None:
            try:
                tmp = self._merge(branch)
            except pygit2.GitError:
                return error("does not merge cleanly", filters)

            self._set_cache(branch, tmp)

        if tmp is True:
            filters.append(FILTER_TEXT.wrap(tag.a(class_="positive_review",
                href=GIT_DIFF_RANGE_URL.format(
                    base=urllib.quote(MASTER_BRANCH, ''),
                    branch=urllib.quote(branch_name, ''))
                )))
        elif tmp is False:
            filters.append(FILTER.attr("class", "positive_review"))
            filters.append(FILTER.attr("title", "already merged"))
        else:
            filters.append(FILTER_TEXT.wrap(tag.a(class_="positive_review",
                href=GIT_DIFF_URL.format(commit=tmp.hex))))

        return apply_filters(filters)

    def _get_cache(self, branch):
        self._create_table()
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute('SELECT base, tmp FROM "merge_store" WHERE target=%s', (branch.hex,))
            try:
                base, tmp = cursor.next()
            except StopIteration:
                return None
        if base != self.master_sha1:
            self._drop_table()
            return None
        if tmp == "True" or tmp == "False":
            return eval(tmp)
        else:
            return self._git.get(tmp)

    def _set_cache(self, branch, tmp):
        self._create_table()
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('DELETE FROM "merge_store" WHERE target=%s', (branch.hex,))
            if tmp is True or tmp is False:
                cursor.execute('INSERT INTO "merge_store" VALUES (%s, %s, %s)', (self.master_sha1, branch.hex, str(tmp)))
            else:
                cursor.execute('INSERT INTO "merge_store" VALUES (%s, %s, %s)', (self.master_sha1, branch.hex, tmp.hex))

    @property
    def master_sha1(self):
        return self._git.lookup_branch(MASTER_BRANCH).get_object().hex

    def _create_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM information_schema.tables WHERE "table_name"=%s', ('merge_store',))
            if not cursor.rowcount:
                cursor.execute('CREATE TABLE "merge_store" ( base text, target text, tmp text, PRIMARY KEY ( target ), UNIQUE ( target, tmp ) )')

    def _drop_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM information_schema.tables WHERE "table_name"=%s', ('merge_store',))
            if cursor.rowcount:
                cursor.execute('DROP TABLE "merge_store"')

    def _merge(self, branch):
        import tempfile
        tmpdir = tempfile.mkdtemp()

        try:
            # libgit2/pygit2 are ridiculously slow when cloning local paths
            subprocess.call(['git', 'clone', self.git_dir, tmpdir, '--branch=%s'%MASTER_BRANCH])

            repo = pygit2.Repository(tmpdir)
            merge = repo.merge(branch.oid)
            if merge.is_fastforward:
                ret = True
            elif merge.is_uptodate:
                ret = False
            else:
                # write the merged tree
                merge_tree = repo.index.write_tree()

                # write objects to main git repo
                def recursive_write(tree):
                    for obj in tree:
                        obj = repo.get(obj.oid)
                        if isinstance(obj, pygit2.Tree):
                            recursive_write(obj)
                        else:
                            self._git.write(pygit2.GIT_OBJ_BLOB, obj.read_raw())
                    return self._git.write(pygit2.GIT_OBJ_TREE, tree.read_raw())
                merge_tree = recursive_write(repo.get(merge_tree))

                ret = self._git.create_commit(
                        None,
                        TRAC_SIGNATURE,
                        TRAC_SIGNATURE,
                        'Temporary merge of %s into %s'%(branch.hex, repo.head.get_object().hex),
                        merge_tree,
                        [repo.head.get_object().oid, branch.oid])
        finally:
            import shutil
            shutil.rmtree(tmpdir)
        return ret

    @property
    def _git(self):
        try:
            return self.__git
        except AttributeError:
            self.__git = pygit2.Repository(self.git_dir)
            return self.__git

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

    def log_table(self, new_commit, limit=None, ignore=[]):
        new_commit = self._git[new_commit]
        table = []
        for commit in self._git.walk(new_commit.oid, pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_TIME):
            if limit is not None and len(table) >= limit:
                break
            if commit.hex in ignore:
                break
            short_sha1 = commit.hex[:7]
            title = commit.message.splitlines()
            if title:
                title = title[0]
            else:
                title = u''
            table.append(u'||[[%s|%s]]||{{{%s}}}||'%(GIT_COMMIT_URL.format(commit=short_sha1), short_sha1, title))
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
                commit = ticket['commit'] = unicode(self._git.lookup_branch(branch).get_object().hex)
            except pygit2.GitError:
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
            except (pygit2.GitError, KeyError):
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
