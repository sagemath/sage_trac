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

class GitError(Exception):
    pass

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

    def filter_stream(self, req, method, filename, stream, data):
        """
        Reformat the ``branch`` field of a ticket to show the history of the
        linked branch.
        """
        if filename == 'ticket.html':
            ticket = data.get('ticket')
            if ticket and ticket.exists:
                branch = ticket['branch']
                if branch:
                    error = None
                    if not self._is_valid_branch_name(branch):
                        error = "not a valid branch name"
                    elif not self._is_existing_branch(branch):
                        error = "branch does not exist"
                    else:
                        try:
                            master = self._dereference_head("master")
                            branch = self._dereference_head(branch)
                            common_ancestor = self._common_ancestor(master,branch)
                            if branch == common_ancestor:
                                # the trac log page does not work if the revisions are equal
                                error = "no commits on branch yet"
                            else:
                                filter = Transformer('//td[@headers="h_branch"]/text()')
                                stream |= filter.wrap(tag.a(href=req.href.log(revs="%s-%s"%(common_ancestor,branch))))
                        except GitError:
                            error = "failed to determine common ancestor with master branch"

                    if error:
                        filter = Transformer('//td[@headers="h_branch"]')
                        stream |= filter.attr("title",error)

        return stream

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
                bool(self.__git("branch","--list",branch).strip()) # the branch exists if theer is something in the output
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
            return self.__git("merge-base",a,b).split('\n')[0]
        except subprocess.CalledProcessError as e:
            self.log.error("%s failed with exit code %s. The output was:\n%s"%(e.cmd,e.returncode,e.output))
            raise GitError("no common ancestor")
        except IndexError:
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
        print ["git","--git-dir=%s"%self.git_dir]+list(args)
        return subprocess.check_output(["git","--git-dir=%s"%self.git_dir]+list(args))

    def ticket_changed(self, ticket, author, comment, old_values):
        """
        If the ``branch`` field of a ticket changes, the ``t/ticket_number``
        should reflect this change:

        - If ``branch`` is now empty or if ``branch`` is invalid, then the
          symbolic ref ``t/ticket_number`` is deleted.

        - If ``branch`` changes, then the symbolic ref ``t/ticket_number``
          points to the new branch.

        """
        if 'branch' not in old_values:
            # branch has not changed
            return

        ticket_ref = "refs/heads/t/%s"%ticket.id
        old_branch = old_values['branch'].strip()
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
