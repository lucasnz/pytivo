import os
import random
import shutil
import sys
import threading
import time
import unicodedata
import urllib

from Cheetah.Filters import Filter
from lrucache import LRUCache

if os.path.sep == '/':
    quote = urllib.quote
    unquote = urllib.unquote_plus
else:
    quote = lambda x: urllib.quote(x.replace(os.path.sep, '/'))
    unquote = lambda x: os.path.normpath(urllib.unquote_plus(x))

class Error:
    CONTENT_TYPE = 'text/html'

def GetPlugin(name):
    try:
        module_name = '.'.join(['plugins', name, name])
        module = __import__(module_name, globals(), locals(), name)
        plugin = getattr(module, module.CLASS_NAME)()
        return plugin
    except ImportError:
        print 'Error no', name, 'plugin exists. Check the type ' \
        'setting for your share.'
        return Error

class EncodeUnicode(Filter):
    def filter(self, val, **kw):
        """Encode Unicode strings, by default in UTF-8"""

        encoding = kw.get('encoding', 'utf8')

        if type(val) == str:
            try:
                val = val.decode('utf8')
            except:
                if sys.platform == 'darwin':
                    val = val.decode('macroman')
                else:
                    val = val.decode('cp1252')
        elif type(val) != unicode:
            val = str(val)
        return val.encode(encoding)

class Plugin(object):

    random_lock = threading.Lock()

    CONTENT_TYPE = ''

    recurse_cache = LRUCache(5)
    dir_cache = LRUCache(10)

    def __new__(cls, *args, **kwds):
        it = cls.__dict__.get('__it__')
        if it is not None:
            return it
        cls.__it__ = it = object.__new__(cls)
        it.init(*args, **kwds)
        return it

    def init(self):
        pass

    def send_file(self, handler, path, query):
        handler.send_content_file(unicode(path, 'utf-8'))

    def get_local_base_path(self, handler, query):
        return os.path.normpath(handler.container['path'])

    def get_local_path(self, handler, query):

        subcname = query['Container'][0]

        path = self.get_local_base_path(handler, query)
        for folder in subcname.split('/')[1:]:
            if folder == '..':
                return False
            path = os.path.join(path, folder)
        return path

    def item_count(self, handler, query, cname, files, last_start=0):
        """Return only the desired portion of the list, as specified by 
           ItemCount, AnchorItem and AnchorOffset. 'files' is either a 
           list of strings, OR a list of objects with a 'name' attribute.
        """
        def no_anchor(handler, anchor):
            handler.server.logger.warning('Anchor not found: ' + anchor)

        totalFiles = len(files)
        index = 0

        if totalFiles and 'ItemCount' in query:
            count = int(query['ItemCount'][0])

            if 'AnchorItem' in query:
                bs = '/TiVoConnect?Command=QueryContainer&Container='
                local_base_path = self.get_local_base_path(handler, query)

                anchor = query['AnchorItem'][0]
                if anchor.startswith(bs):
                    anchor = anchor.replace(bs, '/', 1)
                anchor = unquote(anchor)
                anchor = anchor.replace(os.path.sep + cname, local_base_path, 1)
                if not '://' in anchor:
                    anchor = os.path.normpath(anchor)

                if type(files[0]) == str:
                    filenames = files
                else:
                    filenames = [x.name for x in files]
                try:
                    index = filenames.index(anchor, last_start)
                except ValueError:
                    if last_start:
                        try:
                            index = filenames.index(anchor, 0, last_start)
                        except ValueError:
                            no_anchor(handler, anchor)
                    else:
                        no_anchor(handler, anchor) # just use index = 0

                if count > 0:
                    index += 1

                if 'AnchorOffset' in query:
                    index += int(query['AnchorOffset'][0])

            if count < 0:
                index = (index + count) % len(files)
                count = -count
            files = files[index:index + count]

        return files, totalFiles, index

    def get_files(self, handler, query, filterFunction=None,
                  force_alpha=False, allow_recurse=True):

        class FileData:
            def __init__(self, name, isdir):
                self.name = name
                self.isdir = isdir
                st = os.stat(unicode(name, 'utf-8'))
                self.mdate = st.st_mtime
                self.size = st.st_size

        class SortList:
            def __init__(self, files):
                self.files = files
                self.unsorted = True
                self.sortby = None
                self.last_start = 0

        def build_recursive_list(path, recurse=True):
            files = []
            path = unicode(path, 'utf-8')
            try:
                for f in os.listdir(path):
                    if f.startswith('.'):
                        continue
                    f = os.path.join(path, f)
                    isdir = os.path.isdir(f)
                    if sys.platform == 'darwin':
                        f = unicodedata.normalize('NFC', f)
                    f = f.encode('utf-8')
                    if recurse and isdir:
                        files.extend(build_recursive_list(f))
                    else:
                       if not filterFunction or filterFunction(f, file_type):
                           files.append(FileData(f, isdir))
            except:
                pass
            return files

        subcname = query['Container'][0]
        path = self.get_local_path(handler, query)

        file_type = query.get('Filter', [''])[0]

        recurse = allow_recurse and query.get('Recurse', ['No'])[0] == 'Yes'

        filelist = []
        rc = self.recurse_cache
        dc = self.dir_cache
        if recurse:
            if path in rc and rc.mtime(path) + 300 >= time.time():
                filelist = rc[path]
        else:
            updated = os.path.getmtime(unicode(path, 'utf-8'))
            if path in dc and dc.mtime(path) >= updated:
                filelist = dc[path]
            for p in rc:
                if path.startswith(p) and rc.mtime(p) < updated:
                    del rc[p]

        if not filelist:
            filelist = SortList(build_recursive_list(path, recurse))

            if recurse:
                rc[path] = filelist
            else:
                dc[path] = filelist

        def dir_sort(x, y):
            if x.isdir == y.isdir:
                return name_sort(x, y)
            else:
                return y.isdir - x.isdir

        def name_sort(x, y):
            return cmp(x.name, y.name)

        def date_sort(x, y):
            return cmp(y.mdate, x.mdate)

        def name_sort_rev(x, y):
            return cmp(y.name, x.name)

        def date_sort_rev(x, y):
            return cmp(x.mdate, y.mdate)

        sortby = query.get('SortOrder', ['Normal'])[0].lower()
        if filelist.unsorted or filelist.sortby != sortby:
            if force_alpha:
                filelist.files.sort(dir_sort)
            elif sortby == '!capturedate':
                filelist.files.sort(date_sort)
            elif sortby == 'capturedate':
                filelist.files.sort(date_sort_rev)
            elif sortby == '!normal':
                filelist.files.sort(name_sort_rev)
            else:
                filelist.files.sort(name_sort)

            filelist.sortby = sortby
            filelist.unsorted = False

        files = filelist.files[:]

        # Trim the list
        files, total, start = self.item_count(handler, query, handler.cname,
                                              files, filelist.last_start)
        if len(files) > 1:
            filelist.last_start = start
        return files, total, start
