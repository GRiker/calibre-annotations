#!/usr/bin/env python
# coding: utf-8

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2013, Greg Riker <griker@hotmail.com>'
__docformat__ = 'restructuredtext en'

import imp, inspect, os, re, sys, tempfile, threading, types, urlparse

from functools import partial
from zipfile import ZipFile

try:
    from PyQt5.Qt import (pyqtSignal, Qt, QApplication, QIcon, QMenu, QPixmap,
                          QTimer, QToolButton, QUrl)
except ImportError as e:
    from calibre.devices.usbms.driver import debug_print
    debug_print("Error loading QT5: ", e)
    from PyQt4.Qt import (pyqtSignal, Qt, QApplication, QIcon, QMenu, QPixmap,
                          QTimer, QToolButton, QUrl)

from calibre.constants import DEBUG, isosx, iswindows
#from calibre_plugins.annotations.libimobiledevice import libiMobileDevice, libiMobileDeviceException
from calibre.devices.idevice.libimobiledevice import libiMobileDevice
from calibre.devices.usbms.driver import debug_print

from calibre.ebooks.BeautifulSoup import BeautifulSoup
from calibre.gui2 import Application, open_url
from calibre.gui2.device import device_signals
from calibre.gui2.dialogs.message_box import MessageBox
from calibre.gui2.actions import InterfaceAction
from calibre.library import current_library_name
from calibre.utils.config import config_dir

from calibre_plugins.annotations.annotated_books import AnnotatedBooksDialog
from calibre_plugins.annotations.annotations import merge_annotations, merge_annotations_with_comments
from calibre_plugins.annotations.annotations_db import AnnotationsDB

from calibre_plugins.annotations.common_utils import (CompileUI,
    CoverMessageBox, HelpView, ImportAnnotationsDialog, IndexLibrary,
    Logger, ProgressBar, Struct,
    get_cc_mapping, get_clippings_cid, get_icon, get_pixmap, get_resource_files,
    get_selected_book_mi, plugin_tmpdir,
    set_cc_mapping, set_plugin_icon_resources, updateCalibreGUIView)
#import calibre_plugins.annotations.config as cfg
from calibre_plugins.annotations.config import plugin_prefs
from calibre_plugins.annotations.find_annotations import FindAnnotationsDialog
from calibre_plugins.annotations.message_box_ui import COVER_ICON_SIZE
from calibre_plugins.annotations.reader_app_support import *


# The first icon is the plugin icon, referenced by position.
# The rest of the icons are referenced by name
PLUGIN_ICONS = ['images/annotations.png', 'images/apple.png',
                'images/bluefire_reader.png',
                'images/device_connected.png', 'images/device.png',
                'images/exporting_app.png',
                'images/goodreader.png', 'images/ibooks.png',
                'images/kindle_for_ios.png',
                'images/magnifying_glass.png', 'images/marvin.png',
                'images/matches_hide.png', 'images/matches_show.png',
                'images/stanza.png']


class AnnotationsAction(InterfaceAction, Logger):

    accepts_drops = True
    ios_fs = None
    mount_point = None
    name = 'Annotations'

    SELECT_DESTINATION_MSG = ("Select a book to receive annotations when annotation metadata cannot be matched with library metadata.")
    SELECT_DESTINATION_DET_MSG = (
        "To determine which book will receive incoming annotations, annotation metadata (Title, Author, UUID) is compared to library metadata.\n\n"
        "Annotations whose metadata completely matches library metadata will be added automatically to the corresponding book.\n\n"
        "For partial metadata matches, you will be prompted to confirm the book receiving the annotations.\n\n"
        "If no metadata matches, you will be prompted to confirm the currently selected book to receive the annotations.\n")

    # Declare the main action associated with this plugin
    action_spec = ('Annotations', None,
                   'Import annotations from eBook reader', None)
    action_menu_clone_qaction = True
    popup_type = QToolButton.InstantPopup

    plugin_device_connection_changed = pyqtSignal(object)

    def about_to_show_menu(self):
        self.launch_library_scanner()
        self.rebuild_menus()

    # subclass override
    def accept_enter_event(self, event, md):
        if False:
            for fmt in md.formats():
                self._log("fmt: %s" % fmt)

        if md.hasFormat("text/uri-list"):
            return True
        else:
            return False

    # subclass override
    def accept_drag_move_event(self, event, md):
        if md.hasFormat("text/uri-list"):
            return True
        else:
            return False

    def add_annotations_to_calibre(self, book_mi, annotations_db, cid):
        """
        Add annotations from a single db to calibre
        Update destination Comments or #<custom>
        """
        update_field = get_cc_mapping('annotations', 'field', 'Comments')
        self._log_location(update_field)
        db = self.opts.gui.current_db
        mi = db.get_metadata(cid, index_is_id=True)

        # Get the newly imported annotations
        new_soup = BeautifulSoup(self.opts.db.annotations_to_html(annotations_db, book_mi))

        if update_field == "Comments":
            # Append merged annotations to end of existing Comments

            # Any older annotations?
            comments = db.comments(cid, index_is_id=True)
            if comments is None:
                comments = unicode(new_soup)
            else:
                # Keep comments, update annotations
                comments_soup = BeautifulSoup(comments)
                comments = merge_annotations_with_comments(self, cid, comments_soup, new_soup)
            db.set_comment(mi.id, comments)
        else:
            # Generate annotations formatted for custom field
            # Any older annotations?
            um = mi.metadata_for_field(update_field)
            if mi.get_user_metadata(update_field, False)['#value#'] is None:
                um['#value#'] = unicode(new_soup)
            else:
                # Merge new hashes into old
                old_soup = BeautifulSoup(mi.get_user_metadata(update_field, False)['#value#'])
                merged_soup = merge_annotations(self, cid, old_soup, new_soup)
                um['#value#'] = unicode(merged_soup)
            mi.set_user_metadata(update_field, um)
            db.set_metadata(cid, mi, set_title=False, set_authors=False)
        db.commit()

        #self.gui.library_view.select_rows([cid], using_ids=True)
        updateCalibreGUIView()

        self._log(" annotations updated: '%s' cid:%d " % (mi.title, cid))

    def create_menu_item(self, m, menu_text, image=None, tooltip=None, shortcut=None):
        ac = self.create_action(spec=(menu_text, None, tooltip, shortcut), attr=menu_text)
        if image:
            ac.setIcon(QIcon(image))
        m.addAction(ac)
        return ac

    def describe_confidence(self, confidence, book_mi, library_mi):
        def _title_mismatch():
            msg = "TITLE MISMATCH:\n"
            msg += " library: %s\n" % library_mi.title
            msg += " imported: %s\n" % book_mi.title
            #msg += "Mismatches can occur if the book's metadata has been edited outside of calibre.\n\n"
            return msg

        def _author_mismatch():
            msg = "AUTHOR MISMATCH:\n"
            msg += " library: %s\n" % ', '.join(library_mi.authors)
            msg += " imported: %s\n" % book_mi.author
            #msg += "Mismatches can occur if the book's metadata has been edited outside of calibre.\n\n"
            return msg

        def _uuid_mismatch():
            msg = "UUID MISMATCH:\n"
            msg += " library: %s\n" % library_mi.uuid
            msg += " imported: %s\n" % (book_mi.uuid if book_mi.uuid else 'uuid unavailable')
            #msg += "Mismatches can occur if the book was added from a different installation of calibre.\n\n"
            return msg

        if confidence < 5:
            det_msg = '{:-^45}\n'.format('  confidence: %d  ' % confidence)
            if confidence == 4:
                det_msg = _author_mismatch()
            elif confidence == 3:
                det_msg = _title_mismatch()
                det_msg += _author_mismatch()
            elif confidence == 2:
                det_msg = _uuid_mismatch()
            elif confidence == 1:
                det_msg = _author_mismatch()
                det_msg += _uuid_mismatch()
            elif confidence == 0:
                    det_msg = _title_mismatch()
                    det_msg += _author_mismatch()
                    det_msg += _uuid_mismatch()
        else:
            det_msg = "Metadata matches"
        return det_msg

    # subclass override
    def drop_event(self, event, md):
        mime = "text/uri-list"
        if md.hasFormat(mime):
            self.dropped_url = str(md.data(mime))
            QTimer.singleShot(1, self.do_drop_event)
            return True
        return False

    def do_drop_event(self):
        # Allow the accepted event to process
        QApplication.processEvents()

        self.selected_mi = get_selected_book_mi(self.get_options(),
                                                msg=self.SELECT_DESTINATION_MSG,
                                                det_msg=self.SELECT_DESTINATION_DET_MSG)
        if not self.selected_mi:
            return

        self.launch_library_scanner()
        # Let the library scanner run
        sleep(0.1)
        QApplication.processEvents()

        # Wait for library_scanner to complete
        if self.library_scanner.isRunning():
            self._log("waiting for library_scanner()")
            self.library_scanner.wait()

        path = urlparse.urlparse(self.dropped_url).path.strip()
        scheme = urlparse.urlparse(self.dropped_url).scheme
        path = re.sub('%20', ' ', path)
        self._log_location(path)

        if iswindows:
            if path.startswith('/Shared Folders'):
                path = re.sub(r'\/Shared Folders', 'Z:', path)
            elif path.startswith('/'):
                path = path[1:]
        extension = path.rpartition('.')[2]
        if scheme == 'file' and extension in ['mrv', 'mrvi', 'txt']:
            with open(path) as f:
                raw = f.read()

        # See if anyone can parse it
        exporting_apps = ReaderApp.get_exporting_app_classes()
        for app_class in exporting_apps:
            rac = app_class(self)
            handled = rac.parse_exported_highlights(raw, log_failure=False)
            if handled:
                try:
                    self.present_annotated_books(rac, source="imported")
                except:
                    pass
                break
            else:
                title = "Unable to import annotations"
                msg = ("<p>Unable to import anotations from <tt>{0}</tt>.</p>".format(os.path.basename(path)))
                MessageBox(MessageBox.INFO, title, msg, show_copy_button=False).exec_()
                self._log_location("INFO: %s" % msg)

    def fetch_device_annotations(self, annotated_book_list, source):
        self._log_location(source)
        if annotated_book_list:
            d = AnnotatedBooksDialog(self, annotated_book_list, self.get_annotations_as_HTML, source)
            if d.exec_():
                if d.selected_books:
                    book_count = 0
                    for reader_app in d.selected_books:
                        book_count += len(d.selected_books[reader_app])
                    self.opts.pb.set_maximum(book_count)

                    # Update the progress bar
                    self.opts.pb.set_label("Adding annotations to calibre")
                    self.opts.pb.set_value(0)
                    self.opts.pb.show()

                    updated_annotations = 0

                    try:
                        for reader_app in d.selected_books:
                            Application.processEvents()
                            annotations_db = ReaderApp.generate_annotations_db_name(reader_app, source)
                            updated_annotations += self.process_selected_books(d.selected_books, reader_app, annotations_db)
                    except:
                        import traceback
                        traceback.print_exc()
                        title = "Error fetching annotations"
                        msg = ('<p>Unable to fetch annotations from {0}.</p>'.format(source))
                        det_msg = traceback.format_exc()
                        MessageBox(MessageBox.ERROR, title, msg, det_msg, show_copy_button=False).exec_()
                        self._log_location("ERROR: %s" % msg)
                    self.opts.pb.hide()
                    if updated_annotations:
                        self.report_updated_annotations(updated_annotations)

        else:
            title = "No annotated books found on device"
            msg = ('<p>Unable to find any annotations on {0} matching books in your library.</p>'.format(source))
            MessageBox(MessageBox.INFO, title, msg, show_copy_button=False).exec_()
            self._log_location("INFO: %s" % msg)

    def fetch_ios_annotations(self, an):
        """
        Selectively import annotations from books on mounted iOS device
        """
        self.selected_mi = get_selected_book_mi(self.get_options(),
                                                msg=self.SELECT_DESTINATION_MSG,
                                                det_msg=self.SELECT_DESTINATION_DET_MSG)
        if not self.selected_mi:
            return

        #annotated_book_list = self.get_annotated_books_on_ios_device()
        annotated_book_list = self.get_annotated_books_in_ios_reader_app(an)
        if annotated_book_list:
            self.fetch_device_annotations(annotated_book_list, self.ios.device_name)

    def fetch_usb_device_annotations(self, reader_app):
        """
        Selectively import annotations from books on mounted USB device
        """
        self.selected_mi = get_selected_book_mi(self.get_options(),
                                                msg=self.SELECT_DESTINATION_MSG,
                                                det_msg=self.SELECT_DESTINATION_DET_MSG)
        if not self.selected_mi:
            return

        annotated_book_list = self.get_annotated_books_on_usb_device(reader_app)
        self.fetch_device_annotations(annotated_book_list, self.opts.device_name)

    def find_annotations(self):
        '''
        Launch the Find Annotations dialog, filter GUI to show matching set
        '''
        fa = FindAnnotationsDialog(self.opts)
        if fa.exec_():
            db = self.gui.current_db
            db.set_marked_ids(fa.matched_ids)
            self.gui.search.setEditText('marked:true')
            self.gui.search.do_search()

    # subclass override
    def genesis(self):
        # General initialization, occurs when calibre launches
        self.init_logger()
        self.menus_lock = threading.RLock()
        self.sync_lock = threading.RLock()
        self.connected_device = None
        self.indexed_library = None
        self.library_indexed = False
        self.library_last_modified = None
        self.resources_path = os.path.join(config_dir, 'plugins', 'annotations_resources')

        # Read the plugin icons and store for potential sharing with the config widget
        icon_resources = self.load_resources(PLUGIN_ICONS)
        set_plugin_icon_resources(self.name, icon_resources)

        # Instantiate libiMobileDevice
        try:
            self.ios = libiMobileDevice(
                verbose=plugin_prefs.get('cfg_libimobiledevice_debug_log_checkbox', False))
        except Exception as e:
            self._log_location('ERROR', "Error loading library libiMobileDevice: %s" % (str(e)))
            self.ios = None

        # Build an opts object
        self.opts = self.init_options()

        # Assign our menu to this action and an icon
        self.menu = QMenu(self.gui)
        self.qaction.setMenu(self.menu)
        self.qaction.setIcon(get_icon(PLUGIN_ICONS[0]))
        #self.qaction.triggered.connect(self.main_menu_button_clicked)
        self.menu.aboutToShow.connect(self.about_to_show_menu)

        # Instantiate the database
        db = AnnotationsDB(self.opts, path=os.path.join(config_dir, 'plugins', 'annotations.db'))
        self.opts.conn = db.connect()
        self.opts.db = db

        # Init the prefs file
        self.init_prefs()

        # Load the dynamic reader classes
        self.load_dynamic_reader_classes()

        # Populate dialog resources
        self.inflate_dialog_resources()

        # Compile .ui files as needed
        CompileUI(self)

        # Populate the help resources
        self.inflate_help_resources()

    def generate_confidence(self, book_mi):
        '''
        Match imported metadata against library metadata
        Confidence:
          5: uuid + title + author
          4: uuid + title
          3: uuid
          2: title + author
          1: title
          0:
        Input: {'title':<title>, 'author':<author>, 'uuid':<uuid>}
        Output: cid, confidence_index
        '''

        if self.library_scanner.isRunning():
            self.library_scanner.wait()

        title_map = self.library_scanner.title_map
        uuid_map = self.library_scanner.uuid_map
        confidence = 0
        cid = None

        # Check uuid_map
        if (book_mi['uuid'] in uuid_map and
                book_mi['title'] == uuid_map[book_mi['uuid']]['title'] and
                book_mi['author'] in uuid_map[book_mi['uuid']]['authors']):
            cid = uuid_map[book_mi['uuid']]['id']
            confidence = 5
        elif (book_mi['uuid'] in uuid_map and
              book_mi['title'] == uuid_map[book_mi['uuid']]['title']):
            cid = uuid_map[book_mi['uuid']]['id']
            confidence = 4
        elif book_mi['uuid'] in uuid_map:
            cid = uuid_map[book_mi['uuid']]['id']
            confidence = 3

        # Check title_map
        elif (book_mi['title'] in title_map and
              book_mi['author'] in title_map[book_mi['title']]['authors']):
                cid = title_map[book_mi['title']]['id']
                confidence = 2
        else:
            if book_mi['title'] in title_map:
                cid = title_map[book_mi['title']]['id']
                confidence = 1

        return cid, confidence

    def get_annotated_books_in_ios_reader_app(self, reader_app):
        '''
        '''
        try:
            annotated_book_list = []
            sql_reader_apps = iOSReaderApp.get_sqlite_app_classes(by_name=True)
            reader_app_class = sql_reader_apps[reader_app]

            # Save a reference for merge_annotations
            self.reader_app_class = reader_app_class

            # Instantiate reader_app_class
            ra = reader_app_class(self)
            ra.app_id = self.installed_app_aliases[reader_app]
            self.opts.pb.show()
            ra.open()
            ra.get_installed_books()
            ra.get_active_annotations()
            books_db = ra.generate_books_db_name(reader_app, self.ios.device_name)
            annotations_db = ra.generate_annotations_db_name(reader_app, self.ios.device_name)
            books = ra.get_books(books_db)
            ra.close()
            self.opts.pb.hide()

            if books is None:
                return

            # Get the books for this db
            this_book_list = []
            for book in books:
                book_mi = {}
                for key in book.keys():
                    book_mi[key] = book[key]
                if not book_mi['active']:
                    continue
                annotation_count = self.opts.db.get_annotation_count(annotations_db, book_mi['book_id'])
                last_update = self.opts.db.get_last_update(books_db, book_mi['book_id'], as_timestamp=True)
                if annotation_count:
                    this_book_list.append({
                        'annotations': annotation_count,
                        'author': book_mi['author'],
                        'author_sort': book_mi['author_sort'] if book_mi['author_sort'] else book_mi['author'],
                        'book_id': book_mi['book_id'],
                        'genre': book_mi['genre'],
                        'last_update': last_update,
                        'reader_app': reader_app,
                        'title': book_mi['title'],
                        'title_sort': book_mi['title_sort'] if book_mi['title_sort'] else book_mi['title'],
                        'uuid': book_mi['uuid'],
                        })
            annotated_book_list += this_book_list

        except:
            self.opts.pb.hide()
            import traceback
            traceback.print_exc()
            title = "Error fetching annotations"
            msg = ('<p>Unable to fetch annotations from {0}.</p>'.format(reader_app))
            MessageBox(MessageBox.ERROR, title, msg, show_copy_button=False).exec_()
            self._log_location("ERROR: %s" % msg)

        return annotated_book_list

    def get_annotated_books_on_usb_device(self, reader_app):
        self._log_location()

        annotated_book_list = []

        usb_readers = USBReader.get_usb_reader_classes()
        reader_app_class = usb_readers[reader_app]

        # Save a reference for merge_annotations
        self.reader_app_class = reader_app_class

        # Instantiate reader_app_class
        ra = reader_app_class(self)
        ra.open()
        ra.get_installed_books()
        ra.get_active_annotations()
        books_db = ra.generate_books_db_name(reader_app, self.opts.device_name)
        annotations_db = ra.generate_annotations_db_name(reader_app, self.opts.device_name)
        books = ra.get_books(books_db)
        ra.close()

        #books = self.opts.db.get_books(books_db)
        if books is not None:
            # Get the books for this db
            this_book_list = []
            for book in books:
                book_mi = {}
                for key in book.keys():
                    book_mi[key] = book[key]
                if not book_mi['active']:
                    continue
                annotation_count = self.opts.db.get_annotation_count(annotations_db, book_mi['book_id'])
                last_update = self.opts.db.get_last_update(books_db, book_mi['book_id'], as_timestamp=True)
                if annotation_count:
                    this_book_list.append({
                        'annotations': annotation_count,
                        'author': book_mi['author'],
                        'author_sort': book_mi['author_sort'] if book_mi['author_sort'] else book_mi['author'],
                        'book_id': book_mi['book_id'],
                        'genre': book_mi['genre'],
                        'last_update': last_update,
                        'reader_app': reader_app,
                        'title': book_mi['title'],
                        'title_sort': book_mi['title_sort'] if book_mi['title_sort'] else book_mi['title'],
                        'uuid': book_mi['uuid'],
                        })
            annotated_book_list += this_book_list

        self.opts.pb.hide()
        return annotated_book_list

    def get_annotations_as_HTML(self, annotations_db, book_mi):
        soup = self.opts.db.annotations_to_html(annotations_db, book_mi)
        return soup

    def get_options(self):
        # Defer adding the connected device until we need the information, as it
        # takes some time for the device to be recognized.
        self.opts['device_name'] = None
        if self.connected_device:
            self.opts['device_name'] = self.connected_device.get_device_information()[0]
        self.opts['mount_point'] = self.mount_point
        return self.opts

    def inflate_dialog_resources(self):
        '''
        Copy the dialog files to our resource directory
        '''
        self._log_location()

        dialogs = []
        with ZipFile(self.plugin_path, 'r') as zf:
            for candidate in zf.namelist():
                # Qt UI files
                if candidate.startswith('dialogs/') and candidate.endswith('.ui'):
                    dialogs.append(candidate)
                # Corresponding class definitions
                if candidate.startswith('dialogs/') and candidate.endswith('.py'):
                    dialogs.append(candidate)
        dr = self.load_resources(dialogs)
        for dialog in dialogs:
            if not dialog in dr:
                continue
            fs = os.path.join(self.resources_path, dialog)
            if not os.path.exists(fs):
                # If the file doesn't exist in the resources dir, add it
                if not os.path.exists(os.path.dirname(fs)):
                    os.makedirs(os.path.dirname(fs))
                with open(fs, 'wb') as f:
                    f.write(dr[dialog])
            else:
                # Is the .ui file current?
                update_needed = False
                with open(fs, 'r') as f:
                    if f.read() != dr[dialog]:
                        update_needed = True
                if update_needed:
                    with open(fs, 'wb') as f:
                        f.write(dr[dialog])

    def inflate_help_resources(self):
        '''
        Extract the help resources from the plugin
        '''
        help_resources = []
        with ZipFile(self.plugin_path, 'r') as zf:
            for candidate in zf.namelist():
                if candidate == 'help/help.html' or candidate.startswith('help/images/'):
                    help_resources.append(candidate)

        rd = self.load_resources(help_resources)
        for resource in help_resources:
            if not resource in rd:
                continue
            fs = os.path.join(self.resources_path, resource)
            if os.path.isdir(fs) or fs.endswith('/'):
                continue
            if not os.path.exists(os.path.dirname(fs)):
                os.makedirs(os.path.dirname(fs))
            with open(fs, 'wb') as f:
                f.write(rd[resource])

    def init_logger(self):
        """
        Create the logger with profiling support
        """
        if DEBUG:
            env = 'linux'
            if isosx:
                env = "OS X"
            elif iswindows:
                env = "Windows"
            version = self.interface_action_base_plugin.version
            title = "%s plugin %d.%d.%d" % (self.name, version[0], version[1], version[2])
            self._log("{:~^80}".format(" %s (%s) " % (title, env)))

    def init_options(self, disable_caching=False):
        """
        Build an opts object with a ProgressBar
        """
        opts = Struct(
            disable_caching=plugin_prefs.get('cfg_disable_caching_checkbox', True),
            gui=self.gui,
            icon=get_icon(PLUGIN_ICONS[0]),
            ios = self.ios,
            parent=self,
            prefs=plugin_prefs,
            resources_path=self.resources_path,
            verbose=DEBUG)

        opts['pb'] = ProgressBar(parent=self.gui, window_title=self.name)
        self._log_location("disable_caching: %s" % opts.disable_caching)
        return opts

    def init_prefs(self):
        '''
        Set the initial default values as needed
        '''
        pref_map = {
            #'cfg_annotations_destination_comboBox': 'Comments',
            #'cfg_annotations_destination_field': 'Comments',
            'cfg_news_clippings_lineEdit': 'My News Clippings',
            'developer_mode': False,
            'COMMENTS_DIVIDER': '&middot;  &middot;  &bull;  &middot;  &#x2726;  &middot;  &bull;  &middot; &middot;',
            'HORIZONTAL_RULE': "<hr width='80%' />",
            'plugin_version': "%d.%d.%d" % self.interface_action_base_plugin.version}
        for pm in pref_map:
            if not plugin_prefs.get(pm, None):
                plugin_prefs.set(pm, pref_map[pm])

        # Clean up existing JSON file < v1.3.0
        if plugin_prefs.get('plugin_version', "0.0.0") < "1.3.0":
            self._log_location("Updating prefs to %d.%d.%d" %
                self.interface_action_base_plugin.version)
            for obsolete_setting in ['cfg_annotations_destination_field',
                'cfg_annotations_destination_comboBox']:
                if plugin_prefs.get(obsolete_setting, None) is not None:
                    self._log("removing obsolete entry '{0}'".format(obsolete_setting))
                    plugin_prefs.__delitem__(obsolete_setting)
            plugin_prefs.set('plugin_version', "%d.%d.%d" % self.interface_action_base_plugin.version)

    def import_annotations(self, reader_app):
        """
        Dispatch to reader_app_class handling exported annotations.
        Generate a confidence index 0-4 based on matches
        """
        self._log_location(reader_app)

        supported_reader_apps = ReaderApp.get_reader_app_classes()
        reader_app_class = supported_reader_apps[reader_app]

        # Save a reference for merge_annotations
        self.reader_app_class = reader_app_class

        self.selected_mi = get_selected_book_mi(self.get_options(),
                                                msg=self.SELECT_DESTINATION_MSG,
                                                det_msg=self.SELECT_DESTINATION_DET_MSG)
        if not self.selected_mi:
            return

        ra_confidence = reader_app_class.import_fingerprint

        if ra_confidence or self.selected_mi is not None:
            exporting_apps = iOSReaderApp.get_exporting_app_classes()
            reader_app = exporting_apps[reader_app_class]

            # Open the Import Annotations dialog
            raw_xml = ImportAnnotationsDialog(self, reader_app, reader_app_class).text
            if(raw_xml):
                # Instantiate reader_app_class
                rac = reader_app_class(self)
                success = rac.parse_exported_highlights(raw_xml)
                if not success:
                    self._log("errors parsing raw_xml")

                # Present the imported books, get a list of books to add to calibre
                if rac.annotated_book_list:
                    self.present_annotated_books(rac, source="imported")

    # subclass override
    def initialization_complete(self):
        self.rebuild_menus()

        # Subscribe to device connection events
        device_signals.device_connection_changed.connect(self.on_device_connection_changed)

    def launch_library_scanner(self):
        '''
        Call IndexLibrary() to index current_db by uuid, title
        Need a test to see if db has been updated since last run. Until then,
        optimization disabled.
        After indexing, self.library_scanner.uuid_map and .id_map are populated
        '''
        if (self.library_last_modified == self.gui.current_db.last_modified() and
                self.indexed_library is self.gui.current_db and
                self.library_indexed):
            self._log_location("library index current")
        else:
            self._log_location("updating library index")
            self.library_scanner = IndexLibrary(self)
            self.library_scanner.signal.connect(self.library_index_complete)
            QTimer.singleShot(1, self.start_library_indexing)

    # subclass override
    def library_changed(self, db):
        self._log_location(current_library_name())
        self.library_indexed = False
        self.indexed_library = None
        self.library_last_modified = None

    def library_index_complete(self):
        self._log_location()
        self.library_indexed = True
        self.indexed_library = self.gui.current_db
        self.library_last_modified = self.gui.current_db.last_modified()

    def load_dynamic_reader_classes(self):
        '''
        Load reader classes dynamically from readers/ folder in plugin zip file
        Load additional classes under development from paths specified in config file
        '''
        self._log_location()

        # Load the builtin classes
        folder = 'readers/'
        reader_app_classes = get_resource_files(self.plugin_path, folder=folder)
        sample_classes = ['SampleExportingApp', 'SampleFetchingApp']
        for rac in reader_app_classes:
            basename = re.sub(folder, '', rac)
            name = re.sub('readers/', '', rac).split('.')[0]
            if name in sample_classes:
                continue
            tmp_file = os.path.join(tempfile.gettempdir(), plugin_tmpdir, basename)
            if not os.path.exists(os.path.dirname(tmp_file)):
                os.makedirs(os.path.dirname(tmp_file))
            with open(tmp_file, 'w') as tf:
                tf.write(get_resources(rac))
            self._log(" loading built-in class '%s'" % name)
            imp.load_source(name, tmp_file)
            os.remove(tmp_file)

        # Load locally defined classes specified in config file
        additional_readers = plugin_prefs.get('additional_readers', None)
        sample_path = 'path/to/your/reader_class.py'
        if additional_readers is None:
            # Create an entry for editing
            plugin_prefs.set('additional_readers', [sample_path])
        else:
            for ac in additional_readers:
                if os.path.exists(ac):
                    name = os.path.basename(ac).split('.')[0]
                    name = re.sub('_', '', name)
                    self._log(" loading external class '%s'" % name)
                    try:
                        imp.load_source(name, ac)
                    except:
                        # If additional_class fails to import, exit
                        import traceback
                        traceback.print_exc()
                        raise SystemExit
                elif ac != sample_path:
                    self._log(" unable to load external class from '%s' (file not found)" % ac)

    def main_menu_button_clicked(self):
        '''
        '''
        self.show_configuration()

    def nuke_annotations(self):
        db = self.gui.current_db
        id = db.FIELD_MAP['id']

        # Get all eligible custom fields
        all_custom_fields = db.custom_field_keys()
        self.custom_fields = {}
        for custom_field in all_custom_fields:
            field_md = db.metadata_for_field(custom_field)
            if field_md['datatype'] in ['comments']:
                self.custom_fields[field_md['name']] = {'field': custom_field,
                                                        'datatype': field_md['datatype']}

        fields = ['Comments']
        for cfn in self.custom_fields:
            fields.append(cfn)
        fields.sort()

        # Warn the user that we're going to do it
        title = 'Remove annotations?'
        msg = ("<p>All existing annotations will be removed from %s.</p>" %
               ', '.join(fields) +
               "<p>Proceed?</p>")
        d = MessageBox(MessageBox.QUESTION,
                       title, msg,
                       show_copy_button=False)
        if not d.exec_():
            return
        self._log_location("QUESTION: %s" % msg)

        # Show progress
        pb = ProgressBar(parent=self.gui, window_title="Removing annotations", on_top=True)
        total_books = len(db.data)
        pb.set_maximum(total_books)
        pb.set_value(0)
        pb.set_label('{:^100}'.format("Scanning 0 of %d" % (total_books)))
        pb.show()

        for i, record in enumerate(db.data.iterall()):
            mi = db.get_metadata(record[id], index_is_id=True)
            pb.set_label('{:^100}'.format("Scanning %d of %d" % (i, total_books)))

            # Remove user_annotations from Comments
            if mi.comments:
                soup = BeautifulSoup(mi.comments)
                uas = soup.find('div', 'user_annotations')
                if uas:
                    uas.extract()

                # Remove comments_divider from Comments
                cd = soup.find('div', 'comments_divider')
                if cd:
                    cd.extract()

                # Save stripped Comments
                mi.comments = unicode(soup)

                # Update the record
                db.set_metadata(record[id], mi, set_title=False, set_authors=False,
                                commit=True, force_changes=True, notify=True)

            # Removed user_annotations from custom fields
            for cfn in self.custom_fields:
                cf = self.custom_fields[cfn]['field']
                if True:
                    soup = BeautifulSoup(mi.get_user_metadata(cf, False)['#value#'])
                    uas = soup.findAll('div', 'user_annotations')
                    if uas:
                        # Remove user_annotations from originating custom field
                        for ua in uas:
                            ua.extract()

                        # Save stripped custom field data
                        um = mi.metadata_for_field(cf)
                        stripped = unicode(soup)
                        if stripped == u'':
                            stripped = None
                        um['#value#'] = stripped
                        mi.set_user_metadata(cf, um)

                        # Update the record
                        db.set_metadata(record[id], mi, set_title=False, set_authors=False,
                                        commit=True, force_changes=True, notify=True)
                else:
                    um = mi.metadata_for_field(cf)
                    um['#value#'] = None
                    mi.set_user_metadata(cf, um)
                    # Update the record
                    db.set_metadata(record[id], mi, set_title=False, set_authors=False,
                                    commit=True, force_changes=True, notify=True)

            pb.increment()

        # Hide the progress bar
        pb.hide()

        # Update the UI
        updateCalibreGUIView()

    def on_device_connection_changed(self, is_connected):
        '''
        We need to be aware of what kind of device is connected, whether it's an iDevice
        or a regular USB device.
        self.connected_device is the handle to the driver.
        '''
        self.plugin_device_connection_changed.emit(is_connected)
        if is_connected:
            self.connected_device = self.gui.device_manager.device
            self._log_location(self.connected_device.name)

            # If iDevice, scan for installed reader apps
            if getattr(self.connected_device, 'VENDOR_ID', 0) == [0x05ac]:
                self.installed_app_aliases = iOSReaderApp.get_reader_app_aliases(self)
            else:
                USBReader.get_usb_reader_classes()
        else:
            self._log_location("device disconnected")
            self.connected_device = None
            self.rebuild_menus()

    def present_annotated_books(self, rac, source):
        '''
        Called by importing paths to display book(s) shown in parsed annotations
        '''
        d = AnnotatedBooksDialog(self, rac.annotated_book_list,
                                 self.get_annotations_as_HTML, source=source)
        if d.exec_():
            if d.selected_books:
                self.opts.pb.set_maximum(len(d.selected_books[rac.app_name]))
                self.opts.pb.set_label("Adding annotations to calibre")
                self.opts.pb.set_value(0)
                self.opts.pb.show()
                try:
                    updated_annotations = self.process_selected_books(d.selected_books, rac.app_name, rac.annotations_db)
                except:
                    import traceback
                    traceback.print_exc()
                    title = "Error importing annotations"
                    msg = ('<p>Unable to import annotations from {0}.</p>'.format(rac.app_name))
                    det_msg = traceback.format_exc()
                    MessageBox(MessageBox.ERROR, title, msg, det_msg, show_copy_button=False).exec_()
                    self._log_location("ERROR: %s" % msg)
                self.opts.pb.hide()
                if updated_annotations:
                    self.report_updated_annotations(updated_annotations)

    def process_selected_books(self, selected_books, reader_app, annotations_db):
        '''
        Add annotations arriving from importing classes.
        '''
        self._log_location()
        updated_annotations = 0

        # Are we collecting News clippings?
        collect_news_clippings = plugin_prefs.get('cfg_news_clippings_checkbox', False)
        news_clippings_destination = plugin_prefs.get('cfg_news_clippings_lineEdit', None)

        for book_mi in selected_books[reader_app]:

            # Intercept News clippings
            genres = book_mi['genre'].split(', ')
            if 'News' in genres and collect_news_clippings:
                book_mi['cid'] = get_clippings_cid(self, news_clippings_destination)
                confidence = 5
            else:
                book_mi['cid'], confidence = self.generate_confidence(book_mi)

            if confidence >= 3: # and False: # Uncomment this to force Kobo devices to go through the prompts.
                self.add_annotations_to_calibre(book_mi, annotations_db, book_mi['cid'])
                self._log(" '%s' (confidence: %d) annotations added automatically" % (book_mi['title'], confidence))
                updated_annotations += 1
            else:
                # Low or zero confidence, confirm with user
                if confidence == 0:
                    book_mi['cid'] = self.selected_mi.id
                proposed_mi = self.opts.gui.current_db.get_metadata(int(book_mi['cid']), index_is_id=True)
                title = 'Import annotations • Mismatched metadata'
                msg = ''
                grey = '#ddd'
                if False:
                    grey = '#444'
                    if confidence == 2:
                        msg = '<p>Title Author <span style="color:{grey}">uuid</span>'.format(grey=grey)
                    elif confidence == 1:
                        msg = '<p>Title <span style="color:{grey}">Author uuid</span>'.format(grey=grey)
                    else:
                        msg = '<p><span style="color:{grey}">Title Author uuid</span>'.format(grey=grey)

                # Prep the visual cues for metadata coloring
                found_color = 'black'
                missing_color = '#AAA'

                found = '\u2713'
                missing = '\u2715'
                title_color = author_color = uuid_color = found_color
                title_status = author_status = uuid_status = found
                if confidence <= 2:
                    uuid_color = missing_color
                    uuid_status = missing
                if confidence <= 1:
                    author_color = missing_color
                    author_status = missing
                if confidence == 0:
                    title_color = missing_color
                    title_status = missing

                msg = '''<table width="100%"
                                style="margin:0 30% 0 30%;
                                font-family:'Lucida Console', Monaco, monospace;">
                            <tr>
                                <td align="left" style="color:{title_color};" >{title_status} Title</td>
                                <td align="center" style="color:{author_color}" >{author_status} Author</td>
                                <td align="right" style="color:{uuid_color};" >{uuid_status} UUID</td>
                            </tr>
                        </table>
                        <hr />
                        '''.format(author_color=author_color, title_color=title_color, uuid_color=uuid_color,
                                   author_status=author_status, title_status=title_status, uuid_status=uuid_status)
                msg += (
                    "<p>Add {1} annotations from '{0}' to '{2}' by {3}?</p>".format(
                    book_mi['title'], book_mi['reader_app'],
                    proposed_mi.title, ', '.join(proposed_mi.authors)))

                det_msg = self.describe_confidence(confidence, book_mi, proposed_mi)

                # Get the cover
                db = self.opts.gui.current_db

                cover_path = os.path.join(db.library_path,
                                          db.path(proposed_mi.id, index_is_id=True),
                                          'cover.jpg')
                if not os.path.exists(cover_path):
                    cover_path = I('book.png')
                qpm = QPixmap(cover_path)

                # Show the dialog with destination cover
                d = CoverMessageBox(CoverMessageBox.QUESTION,
                                    title, msg, self.opts, det_msg,
                                    parent=self.opts.gui,
                                    q_icon=QIcon(qpm.scaledToHeight(COVER_ICON_SIZE,
                                                 mode=Qt.SmoothTransformation)),
                                    show_copy_button=False,
                                    default_yes=True)
                if d.exec_() == d.Accepted:
                    self.add_annotations_to_calibre(book_mi, annotations_db, book_mi['cid'])
                    updated_annotations += 1
                    self._log(" '{0}' annotations added to '{2}' with user confirmation (confidence: {1})".format(
                        book_mi['title'], confidence, proposed_mi.title))
                else:
                    self._log(" NO CONFIDENCE: '%s' (confidence: %d), annotations not added to '%s'" %
                            (book_mi['title'], confidence, self.selected_mi.title))
            self.opts.pb.increment()

        return updated_annotations

    def rebuild_menus(self):
        with self.menus_lock:
            m = self.menu
            m.clear()

            # Add 'About…'
            #ac = self.create_menu_item(m, 'About' + '…', image=I("help.png"))
            ac = self.create_menu_item(m, 'About' + '…')
            ac.triggered.connect(self.show_about)
            m.addSeparator()

            # Add the supported reading apps for the connected device
            gui_name = None
            if self.connected_device:
                gui_name = self.connected_device.gui_name

                # Enable iOS reading apps
                if getattr(self.connected_device, 'VENDOR_ID', 0) == [0x05ac]:
                    # Add the fetching options per reader app
                    self.add_sub_menu = m.addMenu(get_icon('images/apple.png'),
                                                  'Fetch annotations from…')
                    self.add_sub_menu.setToolTip('Fetch annotations from iOS reader apps')

                    if self.installed_app_aliases:
                        for an in self.installed_app_aliases:
                            ln = re.sub(' ', '_', an.lower())

                            if an == "Kindle":
                                ln = "kindle_for_ios"

                            # Check for a dedicated icon, else use generic
                            pixmap = get_pixmap("images/%s.png" % ln)
                            if pixmap:
                                icon = get_icon("images/%s.png" % ln)
                            else:
                                icon = get_icon("edit-select-all.png")
                            ac = self.create_menu_item(self.add_sub_menu, an, image=icon)
                            ac.triggered.connect(partial(self.fetch_ios_annotations, an))
                    else:
                        ac = self.create_menu_item(self.add_sub_menu,
                            'no supported reader apps found',
                            image=get_icon('dialog_warning.png'))
                        ac.triggered.connect(self.show_supported_ios_reader_apps)

                else:
                    usb_reader_classes = USBReader.get_usb_reader_classes().keys()
                    primary_name = self.connected_device.name.split()[0]
                    if primary_name in usb_reader_classes:
                        ac = self.create_menu_item(m, 'Fetch annotations from %s' % self.connected_device.gui_name,
                                                   image=get_icon('images/device.png'))
                        ac.triggered.connect(partial(self.fetch_usb_device_annotations, primary_name))

            m.addSeparator()

            # Add the import options
            self.add_sub_menu = m.addMenu(get_icon('images/exporting_app.png'),
                                          'Import annotations from…')
            self.add_sub_menu.setToolTip("Import annotations from iOS reader apps")
            exporting_apps = ReaderApp.get_exporting_app_classes()

            for ios_app_class in exporting_apps:
                an = exporting_apps[ios_app_class]
                ln = re.sub(' ', '_', an.lower())
                # Check for a dedicated icon, else use generic
                pixmap = get_pixmap("images/%s.png" % ln)
                if pixmap:
                    icon = get_icon("images/%s.png" % ln)
                else:
                    icon = get_icon("edit-select-all.png")
                ac = self.create_menu_item(self.add_sub_menu, an, image=icon)
                ac.triggered.connect(partial(self.import_annotations, an))
            m.addSeparator()

            # Add 'Find annotations'
            ac = self.create_menu_item(m, 'Find annotations', image=get_icon('images/magnifying_glass.png'))
            ac.triggered.connect(self.find_annotations)
            m.addSeparator()

            # Add 'Customize plugin…'
            ac = self.create_menu_item(m, 'Customize plugin' + '…', image=I("config.png"))
            ac.triggered.connect(self.show_configuration)

            # Add 'Help'
            ac = self.create_menu_item(m, 'Help', image=I('help.png'))
            ac.triggered.connect(self.show_help)

            # If Alt/Option key pressed, show Developer submenu
            modifiers = Application.keyboardModifiers()
            if bool(modifiers & Qt.AltModifier):
                m.addSeparator()
                self.developer_menu = m.addMenu(QIcon(I('config.png')),
                                                "Developer…")
                action = 'Remove all annotations'
                ac = self.create_menu_item(self.developer_menu, action, image=I('list_remove.png'))
                ac.triggered.connect(self.nuke_annotations)

    def report_updated_annotations(self, updated_annotations):
        suffix = " from 1 book "
        if updated_annotations > 1:
            suffix = " from %d books " % updated_annotations
        msg = "<p>Annotations" + suffix + "added to <b>{0}</b>.</p>".format(get_cc_mapping('annotations', 'combobox', 'Comments'))
        MessageBox(MessageBox.INFO,
                   '',
                   msg=msg,
                   show_copy_button=False,
                   parent=self.gui).exec_()
        self._log_location("INFO: %s" % msg)

    def show_configuration(self):
        self.interface_action_base_plugin.do_user_config(self.gui)

    def show_about(self):
        version = self.interface_action_base_plugin.version
        title = "%s v %d.%d.%d" % (self.name, version[0], version[1], version[2])
        msg = ('<p>To learn more about this plugin, visit the '
               '<a href="http://www.mobileread.com/forums/showthread.php?p=2853161">Annotations plugin thread</a> '
               'at MobileRead’s Calibre forum.</p>')
        text = get_resources('about.txt')
        text = text.decode('utf-8')
        d = MessageBox(MessageBox.INFO, title, msg, det_msg=text, show_copy_button=False)
        d.exec_()

    def show_help(self):
        if False:
            hv = HelpView(self.gui, self.opts.icon, self.opts.prefs,
                          page=os.path.join(self.resources_path, 'help/help.html'), title="Annotations Help")
            hv.show()
        else:
            path = os.path.join(self.resources_path, 'help/help.html')
            open_url(QUrl.fromLocalFile(path))

    def show_supported_ios_reader_apps(self):
        '''
        '''
        supported_reader_apps = sorted(iOSReaderApp.get_reader_app_classes().keys(),
                                       key=lambda s: s.lower())

        title = "Supported iOS reader apps"
        if len(supported_reader_apps) > 1:
            msg = ("The %s plugin supports fetching from %s and %s." %
                   (self.name, ', '.join(supported_reader_apps[:-1]), supported_reader_apps[-1]))
        else:
            msg = ("The %s plugin supports fetching from %s." %
                   (self.name, supported_reader_apps[0]))
        MessageBox(MessageBox.INFO, title, msg, show_copy_button=False).exec_()
        self._log_location("INFO: %s" % msg)

    # subclass override
    def shutting_down(self):
        self._log_location()
        return True

    def start_library_indexing(self):
        self.library_scanner.start()
