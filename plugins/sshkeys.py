from trac.core import *
from trac.web.chrome import *
from trac.util.translation import gettext as _
from trac.prefs import IPreferencePanelProvider
from trac.admin.api import IAdminCommandProvider
from trac.util.text import printout
from tracrpc.api import IXMLRPCHandler

class UserDataStore(Component):
    def save_data(self, user, dictionary):
        """
        Saves user data for user.
        """
        self._create_table()
        with self.env.db_transaction as db:
	    cursor = db.cursor()
	    for key, value in dictionary.iteritems():
	        try:
                    cursor.execute('INSERT INTO "user_data_store" VALUES (%s, %s, %s)', (user, key, value))
		except:
		    cursor.execute('REPLACE INTO "user_data_store" VALUES (%s, %s, %s)', (user, key, value)) 

    def get_data(self, user):
        """
        Returns a dictionary with all data keys
        """
        self._create_table()
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute('SELECT key, value FROM "user_data_store" WHERE "user"=%s', (user,))
            return {key:value for key, value in cursor}

    def get_data_all_users(self):
        """
        Returns a dictionary with all data keys
        """
        self._create_table()
        with self.env.db_query as db:
            cursor = db.cursor()
            return_value = {}
            cursor.execute('SELECT "user", key, value FROM "user_data_store"')
            for user, key, value in cursor:
                if return_value.has_key(user):
                    return_value[user][key] = value
                else:
                    return_value[user] = {key: value}
            return return_value

    def _create_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
	    try:
                cursor.execute('CREATE TABLE "user_data_store" ( "user" text, key text, value text, UNIQUE ( "user", key ) )')
            except Exception as e:
                print e

class SshKeysPlugin(Component):
    implements(IPreferencePanelProvider, IAdminCommandProvider, IXMLRPCHandler)
    
    def __init__(self):
        self._user_data_store = UserDataStore(self.compmgr)

    # IPreferencePanelProvider methods
    def get_preference_panels(self, req):
        yield ('sshkeys', _('SSH keys'))
    
    def render_preference_panel(self, req, panel):
        user = req.authname
        if req.method == 'POST':
            new_ssh_key = req.args.get('ssh_key').strip()
            if new_ssh_key:
                self._setkey(user, new_ssh_key)
                add_notice(req, 'Your ssh key has been saved.')
            req.redirect(req.href.prefs(panel or None))
    
        return 'prefs_ssh_keys.html', self._user_data_store.get_data(user)

    # IAdminCommandProvider methods
    def get_admin_commands(self):
        yield ('sshkeys listusers', '',
               'Get a list of users that have a SSH key registered',
               None, self._do_listusers)
        yield ('sshkeys dumpkey', '<user>',
               "export the <user>'s SSH key to stdout",
               None, self._do_dump_key)

    # AdminCommandProvider boilerplate

    def _do_listusers(self):
         for user in self._listusers():
              printout(user)

    def _do_dump_key(self, user):
        printout(self._getkey(user))

    # general functionality
    def _listusers(self):
        all_data = self._user_data_store.get_data_all_users()
        for user, data in all_data.iteritems():
            if data.has_key('ssh_key'):
                yield user

    def _getkey(self, user):
        return self._user_data_store.get_data(user)['ssh_key']

    def _setkey(self, user, key):
        self._user_data_store.save_data(user, {'ssh_key': key})

    # RPC boilerplate
    def listusers(self, req):
        return list(self._listusers())

    def getkey(self, req):
        return self._getkey(req.authname)

    def setkey(self, req, key):
        return self._setkey(req.authname, key)

    # IXMLRPCHandler methods
    def xmlrpc_namespace(self):
        return "sshkeys"

    def xmlrpc_methods(self):
        yield (None, ((list,),), self.listusers)
        yield (None, ((str,),), self.getkey)
        yield (None, ((None,str),), self.setkey)

