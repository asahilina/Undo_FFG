#!python3.6
# Undo_MoM2e.py - Undo for Mansions of Madness Second Edition Windows app
# Copyright (C) 2017 Christopher Gurnee
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


import threading, io, hashlib, collections, json, shutil, contextlib, \
       ctypes, ctypes.wintypes, sys, os, time, traceback
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, BadZipFile
from multiprocessing.connection import Listener, Client
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import nrbf

__version__ = '1.0'
DEFAULT_MAX_UNDO_STATES = 20


MOM = 'MoM'
DEFAULT_GAME = MOM
if __name__ == '__main__':
    if len(sys.argv) > 1:
        if len(sys.argv) == 2 and sys.argv[1].startswith('--game='):
            if sys.argv[1][7:].lower() == 'mom':
                FFG_GAME = MOM
            else:
                sys.exit(f'Unsupported game, must be one of: mom')
        else:
            sys.exit(f'Usage: {sys.argv[0]} [--game=mom]')
    else:
        FFG_GAME = DEFAULT_GAME
else:
    FFG_GAME = DEFAULT_GAME


# Return the binary hash of the files inside a MoM SaveGame directory
EMPTY_BINHASH = hashlib.md5().digest()
def dir_binhash(directory):
    hash = hashlib.md5()
    for f in directory.iterdir():
        if f.stem.lower() != 'log' and f.is_file():  # exclude the Log file
            hash.update(f.read_bytes())
    return hash.digest()

# Helper functions for validating Windows API return values
def returned_invalid_handle(result, func, arguments):
    if result == 0xFFFF_FFFF or result == 0xFFFF_FFFF_FFFF_FFFF:
        raise ctypes.WinError()
    return result
def returned_false(result, func, arguments):
    if result == 0:
        raise ctypes.WinError()
    return result

# Watch the specified directory for any file changes, and call the callback once they've been completed
watcher_skip_next = False  # if True and the next change takes 0.5s or less to finish, it is skipped
def watch_directory(directory, callback):
    try:
        # Load the Windows API functions and constants we need
        t = ctypes.wintypes
        FindFirstChangeNotification = ctypes.windll.kernel32.FindFirstChangeNotificationW
        FindFirstChangeNotification.argtypes = t.LPCWSTR, t.BOOL, t.DWORD
        FindFirstChangeNotification.restype  = t.HANDLE
        FindFirstChangeNotification.errcheck = returned_invalid_handle
        FALSE = t.BOOL(0)
        FILE_NOTIFY_CHANGE_FILE_NAME_or_LAST_WRITE = t.DWORD(0x0000_0001 | 0x0000_0010)
        #
        FindCloseChangeNotification = ctypes.windll.kernel32.FindCloseChangeNotification
        FindCloseChangeNotification.argtypes = t.HANDLE,
        FindCloseChangeNotification.restype  = t.BOOL
        FindCloseChangeNotification.errcheck = returned_false
        #
        WaitForSingleObject = ctypes.windll.kernel32.WaitForSingleObject
        WaitForSingleObject.argtypes = t.HANDLE, t.DWORD
        WaitForSingleObject.restype  = t.DWORD
        WaitForSingleObject.errcheck = returned_invalid_handle
        INFINITE = t.DWORD(0xFFFF_FFFF)

        global watcher_skip_next
        directory_arg = t.LPCWSTR(str(directory))
        while True:
            handle = FindFirstChangeNotification(directory_arg, FALSE, FILE_NOTIFY_CHANGE_FILE_NAME_or_LAST_WRITE)
            try:
                WaitForSingleObject(handle, INFINITE)
            finally:
                FindCloseChangeNotification(handle)

            # Wait until files remain unchanged for a half-second stretch (but at least one second)
            time.sleep(0.5)
            if watcher_skip_next:
                watcher_skip_next = False
                continue
            last_binhash = dir_binhash(directory)
            while True:
                time.sleep(0.5)
                cur_binhash = dir_binhash(directory)
                if cur_binhash == last_binhash:
                    break
                last_binhash = cur_binhash
            callback(cur_binhash)

    except BaseException:
        callback(error=sys.exc_info())
        raise


# Loads the CreateFile Windows API function and some constants we need
def load_CreateFile():
    global CreateFile, CloseHandle, GENERIC_READ_and_WRITE, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
    t = ctypes.wintypes
    CreateFile = ctypes.windll.kernel32.CreateFileW
    CreateFile.argtypes = t.LPCWSTR, t.DWORD, t.DWORD, t.LPVOID, t.DWORD, t.DWORD, t.HANDLE
    CreateFile.restype  = t.HANDLE
    CreateFile.errcheck = returned_invalid_handle
    GENERIC_READ_and_WRITE = t.DWORD(0x8000_0000 | 0x4000_0000)
    OPEN_EXISTING          = t.DWORD(3)
    FILE_ATTRIBUTE_NORMAL  = t.DWORD(0x80)
    #
    CloseHandle = ctypes.windll.kernel32.CloseHandle
    CloseHandle.argtypes = t.HANDLE,
    CloseHandle.restype  = t.BOOL
    CloseHandle.errcheck = returned_false
load_CreateFile()
#
# Return True iff the specified file can be opened *exclusively* for read/write
# (exists and isn't opened by another process)
def can_open_exclusively(filepath):
    try:
        handle = CreateFile(
            str(filepath),           # lpFileName
            GENERIC_READ_and_WRITE,  # dwDesiredAccess
            0,                       # dwShareMode (0 == sharing not permitted)
            None,                    # lpSecurityAttributes
            OPEN_EXISTING,           # dwCreationDisposition
            FILE_ATTRIBUTE_NORMAL,   # dwFlagsAndAttributes
            None)                    # hTemplateFile
    except WindowsError as e:
        if e.winerror == 32:  # "The process cannot access the file because it is being used by another process."
            return False
        raise  # unexpected error
    CloseHandle(handle)
    return True


@contextlib.contextmanager
def ignored(*exceptions):
    try: yield
    except exceptions: pass

# Read the contents of a GameData.dat file to retrieve the scenario name,
# player count, and round number (ignoring errors resulting from format changes)
def parse_gamedata(savefile):
    savedata = nrbf.read_stream(savefile)
    scenario = players = round = ''
    # Remove the last word of the VariantName (I suspect it's the map variant):
    with ignored(AttributeError): scenario = savedata.VariantName[:savedata.VariantName.rfind(' ')]
    # InvestigatorIds is a comma-separated string; count its values:
    with ignored(AttributeError): players = savedata.InvestigatorIds.count(',') + 1
    with ignored(AttributeError): round   = savedata.Round
    return scenario, players, round

# Read the contents of a MoM_SaveGame file to retrieve the tile count
# and monster count (ignoring errors resulting from format changes)
def parse_savegame(savefile):
    savedata = nrbf.read_stream(savefile)
    tiles = 0
    try:
        for tile in savedata.TileSaveData.values():
            tiles += 1 if tile.Visible else 0
    except AttributeError:
        tiles = ''
    monsters = 0
    try:
        for node in savedata.NodeSaveData.values():
            if type(node).__name__ == 'FFG_MoM_MoM_SavedNodeMonster':
                # Generated monsters are always visible, and of course so are
                # 'Visible' ones, but either is only present if it's still alive.
                if (node.WasGenerated or node.Visible) and \
                   (node.DamageCount < node.MaxDamage):
                    monsters += 1
    except AttributeError:
        monsters = ''
    return tiles, monsters


# Directory & filename constants
APPDATA_DIR = Path(os.environ['APPDATA'])
MYDATA_DIR  = APPDATA_DIR / 'Undo for MoM2e'
SETTINGS_FILENAME = MYDATA_DIR / 'settings.json'
if FFG_GAME == MOM:
    SAVEGAME_DIR = APPDATA_DIR / r'..\LocalLow\Fantasy Flight Games\Mansions of Madness Second Edition\SavedGame'

# Game-specific GUI strings
if FFG_GAME == MOM:
    GAME_NAME_TEXT  = 'Mansions of Madness'
    OPEN_BUTON_TEXT = 'Open Mansions\nof Madness'

# Settings (all one of them)
settings = {}
MAX_UNDO_STATES = 'max_undo_states'

# A "binhash" is a bytes object containing the output from dir_binhash().
# A "hexhash" is string containing the first HEXHASH_LEN hex digits of the binhash;
# it is used in known_undostate_hexhashes, Undo State filenames, and treeview Item IDs.
HEXHASH_LEN        = 10
binhash_to_hexhash = lambda binhash: binhash.hex()[:HEXHASH_LEN]
EMPTY_HEXHASH      = binhash_to_hexhash(EMPTY_BINHASH)

CURRENT_ARROW = '\u2190Current'  # '<--Current'


# Override the exception handler for Tk events to cause the app to die (they're normally suppressed)
class UndoRoot(tk.Tk):
    def report_callback_exception(self, *error):
        msg = ''.join(traceback.format_exception(*error))
        print(msg, file=sys.stderr)
        messagebox.showerror('Exception', msg)
        self.destroy()

# The main application window
class UndoApplication(ttk.Frame):

    def __init__(self, master = None):
        super().__init__(master)
        self.master.title(f'Undo v{__version__} for {GAME_NAME_TEXT}')
        self.master.iconbitmap('Undo_MoM2e.ico')

        # Frame for treeview-related widgets
        frame = ttk.Frame()
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=tk.TRUE, padx=12, pady=12)

        ttk.Label(frame, text='Undo States', underline=0).pack(padx=3, pady=3, anchor=tk.W)

        col_headings = ('Scenario', 'Players', 'Round', 'Tiles', 'Monsters', 'Timestamp')
        self.states_treeview = ttk.Treeview(frame,
            columns    = [c.lower() for c in col_headings] + ['current'],
            height     = min(settings[MAX_UNDO_STATES], 40),
            selectmode = 'browse',   # only one item at a time may be selected
            show       = 'headings'  # don't show the item ID (a hexhash)
        )
        for col in col_headings:
            self.states_treeview.heading(col.lower(), text=col)
            if col not in ('Scenario', 'Timestamp'):
                self.states_treeview.column(col.lower(), anchor=tk.CENTER, width=60)
        self.states_treeview.column('scenario',  anchor=tk.E)
        self.states_treeview.column('scenario',  width=160)
        self.states_treeview.column('timestamp', width=120)
        self.states_treeview.column('current',   width=60)
        self.states_treeview.tag_configure('current_tag', background='yellow')
        self.states_treeview.bind('<<TreeviewSelect>>', self.handle_state_selected)
        self.states_treeview.pack(side=tk.LEFT, fill=tk.BOTH, expand=tk.TRUE)
        self.states_treeview.focus_set()
        self.master.bind('<Alt_L><u>', lambda e: self.states_treeview.focus_set())
        self.master.bind('<Alt_R><u>', lambda e: self.states_treeview.focus_set())

        scrollbar = ttk.Scrollbar(frame, command=self.states_treeview.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.states_treeview.config(yscrollcommand=scrollbar.set)

        # Frame for Button widgets
        frame = ttk.Frame()
        frame.pack(side=tk.LEFT, fill=tk.Y, pady=30)

        ttk.Style().configure('TButton', justify='center')
        button_pady = 6

        # This is the only button whose command isn't a member function
        # (just to group the savegame-related code together down below)
        self.restore_button = ttk.Button(frame, text='Restore selected\nUndo State',
            command=handle_restore_clicked, underline=0, state=tk.DISABLED)
        self.restore_button.pack(fill=tk.X, pady=button_pady)
        self.master.bind('<Alt_L><r>', lambda e: self.restore_button.invoke())
        self.master.bind('<Alt_R><r>', lambda e: self.restore_button.invoke())

        self.save_as_button = ttk.Button(frame, text='Save selected\nUndo State as...',
            command=self.handle_save_as_clicked, underline=0, state=tk.DISABLED)
        self.save_as_button.pack(fill=tk.X, pady=button_pady)
        self.master.bind('<Alt_L><s>', lambda e: self.save_as_button.invoke())
        self.master.bind('<Alt_R><s>', lambda e: self.save_as_button.invoke())

        restore_from_button = ttk.Button(frame, text='Restore saved\nUndo State from...',
            command=self.handle_restore_from_clicked, underline=25)
        restore_from_button.pack(fill=tk.X, pady=button_pady)
        self.master.bind('<Alt_L><f>', lambda e: restore_from_button.invoke())
        self.master.bind('<Alt_R><f>', lambda e: restore_from_button.invoke())

        settings_button = ttk.Button(frame, text='Settings...',
            command=self.handle_settings_clicked, underline=2)
        settings_button.pack(side=tk.BOTTOM, fill=tk.X, pady=button_pady)
        self.master.bind('<Alt_L><t>', lambda e: settings_button.invoke())
        self.master.bind('<Alt_R><t>', lambda e: settings_button.invoke())

        open_mom_button = ttk.Button(frame, text=OPEN_BUTON_TEXT,
            command=self.handle_open_mom_clicked, underline=0)
        open_mom_button.pack(side=tk.BOTTOM, fill=tk.X, pady=button_pady)  # gets placed *above* the settings button,
        open_mom_button.lower(settings_button)                             # so move its tab-stop before settings too
        self.master.bind('<Alt_L><o>', lambda e: open_mom_button.invoke())
        self.master.bind('<Alt_R><o>', lambda e: open_mom_button.invoke())

        ttk.Sizegrip().pack(side=tk.BOTTOM)

    def handle_state_selected(self, event):
        new_state = tk.NORMAL if event.widget.selection() else tk.DISABLED
        self.restore_button.config(state=new_state)
        self.save_as_button.config(state=new_state)

    FILEDIALOG_ARGS = dict(
        filetypes        = ((f'{GAME_NAME_TEXT} Undo files', '*.undo'), ('All files', '*')),
        defaultextension = '.undo')

    @classmethod
    def handle_save_as_clicked(cls):
        selected = app.states_treeview.selection()
        assert selected and len(selected) == 1
        hexhash   = selected[0]
        scenario  = app.states_treeview.set(hexhash, 'scenario')
        players   = app.states_treeview.set(hexhash, 'players')
        round     = app.states_treeview.set(hexhash, 'round')
        timestamp = app.states_treeview.set(hexhash, 'timestamp')
        filename  = ''
        if scenario:
            filename += scenario
            if players:
                filename += f' ({players}p)'
            filename += ', '
        elif players:
            filename += f'{players} players, '
        if round:
            filename += f'round {round}, '
        filename += timestamp[:-3].replace(':', '.')
        filename = filedialog.asksaveasfilename(title='Save Undo State as', initialfile=filename, **cls.FILEDIALOG_ARGS)
        if filename:
            glob_pattern = f'????-??-?? ??.??.?? {hexhash}.zip'
            src_filename = next(MYDATA_DIR.glob(glob_pattern))  # next() gets the first (should be the only) filename
            shutil.copyfile(src_filename, filename)

    @classmethod
    def handle_restore_from_clicked(cls):
        if is_mom_running():
            return
        filename = filedialog.askopenfilename(title='Restore Undo State from', **cls.FILEDIALOG_ARGS)
        if not filename or is_mom_running():
            return

        # Ensure it's a valid Undo file
        try:
            with ZipFile(filename) as unzipper:
                for zipped_filename in unzipper.namelist():
                    zipped_filename = zipped_filename.lower()
                    if zipped_filename in ('gamedata.dat', 'mom_savegame'):
                        break  # if at least one of the savegame files is present, assume it's valid
                else:
                    raise BadZipFile("can't find either GameData.dat or MoM_SaveGame")
        except BadZipFile as e:
            messagebox.showerror('Error', f'This file is not a {GAME_NAME_TEXT} Undo file.\n({e})')
            return

        restore_undo_state(filename)
        handle_new_savegame()

    @staticmethod
    def handle_open_mom_clicked():
        root.config(cursor='wait')
        os.startfile('steam://run/478980')  # see https://developer.valvesoftware.com/wiki/Steam_browser_protocol
        root.config(cursor='')

    @staticmethod
    def handle_settings_clicked():
        new_max_undo_states = simpledialog.askinteger('Settings', 'Maximum Undo States:',
            initialvalue=settings[MAX_UNDO_STATES], minvalue=2, maxvalue=10_000)
        if new_max_undo_states and new_max_undo_states != settings[MAX_UNDO_STATES]:
            cur_undo_states = len(known_undostate_hexhashes)
            if new_max_undo_states < cur_undo_states:
                diff = cur_undo_states - new_max_undo_states
                answered_yes = messagebox.askyesno('Delete Undo States?',
                    f'Decreasing the maximum Undo States below their current number will delete {"some" if diff>1 else "one"} of them. '
                    f'Are you sure you want to continue and delete the {f"{diff} oldest States" if diff>1 else "oldest State"}?',
                    icon=messagebox.WARNING, default=messagebox.NO)
                if not answered_yes:
                    return
            settings[MAX_UNDO_STATES] = new_max_undo_states
            with SETTINGS_FILENAME.open('w') as settings_file:
                json.dump(settings, settings_file)
            trim_undo_states()


# Load the settings saved right above
def load_settings():
    global settings
    if SETTINGS_FILENAME.is_file():
        with SETTINGS_FILENAME.open() as settings_file:
            settings = json.load(settings_file)


# Load the preserved savegames from our data directory (done once at program start),
# returning False if the current SaveGame hasn't yet been seen & preserved
#
# set of hexhashes of Undo States currently both in MYDATA_DIR and displayed in the UI:
known_undostate_hexhashes = collections.OrderedDict()
def load_undo_states():
    assert len(known_undostate_hexhashes)          == 0
    assert len(app.states_treeview.get_children()) == 0
    cur_savegame_found   = False
    cur_savegame_hexhash = binhash_to_hexhash(dir_binhash(SAVEGAME_DIR))

    zip_filenames = list(MYDATA_DIR.glob(f'????-??-?? ??.??.?? {"?"*HEXHASH_LEN}.zip'))
    zip_filenames.sort()  # sorts from oldest to newest
    for zip_filename in zip_filenames:
        scenario = players = round = tiles = monsters = ''
        hexhash       = str(zip_filename.stem)[-HEXHASH_LEN:]
        timestamp_str = str(zip_filename.name)[:19].replace('.', ':')
        if hexhash in known_undostate_hexhashes:  # shouldn't happen often if ever
            continue
        known_undostate_hexhashes[hexhash] = True

        # Parse the savegame files inside the zip for display purposes
        with ZipFile(zip_filename) as zipper:
            for zipped_filename in zipper.namelist():
                if zipped_filename.lower() == 'gamedata.dat':
                    with zipper.open(zipped_filename) as savefile:
                        scenario, players, round = parse_gamedata(savefile)
                elif zipped_filename.lower() == 'mom_savegame':
                    with zipper.open(zipped_filename) as savefile:
                        tiles, monsters = parse_savegame(savefile)

        # Insert the Undo State into the treeview UI at the top
        app.states_treeview.insert('', 0, hexhash,
            values=(scenario, players, round, tiles, monsters, timestamp_str, ''))
        if hexhash == cur_savegame_hexhash:
            app.states_treeview.set(hexhash, 'current', CURRENT_ARROW)
            app.states_treeview.item(hexhash, tags=('current_tag',))
            cur_savegame_found = True

    # Don't know why, but if an item inside the treeview isn't given focus,
    # one can't use tab alone (w/o a mouse) to give focus to the treeview
    if len(known_undostate_hexhashes):
        app.states_treeview.focus(hexhash)  # the most recently added (top) item

    assert len(known_undostate_hexhashes) == len(app.states_treeview.get_children())
    return cur_savegame_found or cur_savegame_hexhash == EMPTY_HEXHASH


# Callback which runs in the context of the directory watcher
# thread and is called when a new savegame is detected
watcher_thread_error = None
def send_new_savegame_event(binhash = None, error = None):
    if error:
        global watcher_thread_error
        watcher_thread_error = error
        root.event_generate('<<watcher_error>>')
    else:
        assert binhash
        if binhash_to_hexhash(binhash) not in known_undostate_hexhashes:
            root.event_generate('<<new_savegame>>')

# Runs in the context of the main thread to handle watcher thread exceptions
def handle_watcher_error(event):
    global watcher_thread_error
    msg = ''.join(traceback.format_exception(*watcher_thread_error)) if watcher_thread_error \
          else 'An unknown directory watcher error occurred.'
    watcher_thread_error = None
    print(msg, file=sys.stderr)
    messagebox.showerror('Exception', msg)
    root.destroy()

# Runs in the context of the main thread to handle new savegames
# and also on start to preserve an existing savegame
settings[MAX_UNDO_STATES] = DEFAULT_MAX_UNDO_STATES  # can be overwritten later in load_settings()
def handle_new_savegame(event = None):
    # Iterate through the files in the MoM SaveGame directory to:
    #   - zip them (in memory for now) into an "Undo State"
    #   - calculate their hexhash to name the Undo State
    #   - parse the savegame files for display purposes
    inmem_zip = io.BytesIO()
    zipper    = ZipFile(inmem_zip, 'w', compression=ZIP_DEFLATED)
    hash      = hashlib.md5()
    scenario = players = round = tiles = monsters = ''
    for f in SAVEGAME_DIR.iterdir():
        if f.is_file():
            zipper.write(f, f.name)
            if f.stem.lower() == 'log':
                continue
            hash.update(f.read_bytes())
            if f.name.lower() == 'gamedata.dat':
                with f.open('rb') as savefile:
                    scenario, players, round = parse_gamedata(savefile)
            elif f.name.lower() == 'mom_savegame':
                with f.open('rb') as savefile:
                    tiles, monsters = parse_savegame(savefile)
    zipper.close()
    binhash = hash.digest()

    for id in app.states_treeview.tag_has('current_tag'):  # for all w/the current_tag:
        app.states_treeview.set(id, 'current', '')         # erase its 'current' column
        app.states_treeview.item(id, tags=())              # and remove the tag

    if binhash == EMPTY_BINHASH:
        return
    hexhash = binhash_to_hexhash(binhash)

    if hexhash in known_undostate_hexhashes:  # can happen following handle_restore_from_clicked()
        app.states_treeview.set(hexhash, 'current', CURRENT_ARROW)  # set the 'current' column
        app.states_treeview.item(hexhash, tags=('current_tag',))    # and add the tag
    else:
        timstamp_str = time.strftime('%Y-%m-%d %H:%M:%S')  # e.g. '2017-11-08 19:01:27'
        zip_filename = MYDATA_DIR / f'{timstamp_str.replace(":", ".")} {hexhash}.zip'
        zip_filename.write_bytes(inmem_zip.getbuffer())
        app.states_treeview.insert('', 0, hexhash, tags=('current_tag',),
            values=(scenario, players, round, tiles, monsters, timstamp_str, CURRENT_ARROW))
        known_undostate_hexhashes[hexhash] = True
        # Don't know why, but if an item inside the treeview isn't given focus,
        # one can't use tab alone (w/o a mouse) to give focus to the treeview
        if len(known_undostate_hexhashes) == 1:  # only need to do this once
            app.states_treeview.focus(hexhash)
    app.states_treeview.see(hexhash)

    trim_undo_states()

# Delete Undo States if we're over the max
def trim_undo_states():
    assert settings[MAX_UNDO_STATES] > 0
    while len(known_undostate_hexhashes) > settings[MAX_UNDO_STATES]:
        hexhash = known_undostate_hexhashes.popitem(last=False)[0]  # removes and returns the oldest
        glob_pattern = f'????-??-?? ??.??.?? {hexhash}.zip'
        for zip_filename in MYDATA_DIR.glob(glob_pattern):          # should be exactly one
            zip_filename.unlink()
        app.states_treeview.delete(hexhash)


# If MoM is being played, alert the user and return True, else return False
def is_mom_running():
    log_filename = SAVEGAME_DIR / 'Log'
    if log_filename.is_file() and not can_open_exclusively(log_filename):
        messagebox.showwarning('Error', 'Please save your game and quit to the main\n'
                                        'menu in order to restore an Undo State.')
        return True
    return False

# Restore an Undo State into the MoM SaveGame directory and update the UI
def handle_restore_clicked():
    if is_mom_running():
        return

    selected = app.states_treeview.selection()
    assert selected and len(selected) == 1
    hexhash = selected[0]
    app.states_treeview.selection_set()  # clear the selection

    glob_pattern = f'????-??-?? ??.??.?? {hexhash}.zip'
    zip_filename = next(MYDATA_DIR.glob(glob_pattern))  # next() gets the first (should be the only) filename
    assert zip_filename
    restore_undo_state(zip_filename)

    for id in app.states_treeview.tag_has('current_tag'):  # for all w/the current_tag:
        app.states_treeview.set(id, 'current', '')         # erase its 'current' column
        app.states_treeview.item(id, tags=())              # and remove the tag
    app.states_treeview.set(hexhash, 'current', CURRENT_ARROW)    # set the 'current' column
    app.states_treeview.item(hexhash, tags=('current_tag',))      # and add the tag

def restore_undo_state(zip_filename):
    global watcher_skip_next
    extracted_filenames = []
    try:
        with ZipFile(zip_filename) as unzipper:
            watcher_skip_next = True  # tell the directory watcher thread to skip the following changes
            for zipped_filename in unzipper.namelist():
                if zipped_filename == Path(zipped_filename).name:  # ensures we're unzipping to only the SaveGame dir
                    unzipper.extract(zipped_filename, SAVEGAME_DIR)
                    extracted_filenames.append(zipped_filename)
    except Exception:
        # Undo any file extractions if there were any errors
        for filename in extracted_filenames:
            filename = SAVEGAME_DIR / filename
            try:
                filename.unlink()
            except Exception:
                traceback.print_exc()
        raise


# Whenever the pipe receives a connection, restore the main window (see right below)
def restore_window_listener(pipe):
    while True:
        pipe.accept().close()
        root.deiconify()
        root.attributes('-topmost', 1)  # root.lift() doesn't work on modern
        root.attributes('-topmost', 0)  # versions of Windows, but this does

root = app = None
def main():
    global root, app
    exclusive_pipe = None
    try:
        # Create a named pipe. When running a second instance of Undo_MoM2e, the pipe
        # creation will fail. If it does, instead connect to the already-existing
        # pipe which causes the first instance of Undo_MoM2e to restore its window.
        PIPE_NAME = r'\\.\pipe\Undo_MoM2e'
        try:
            exclusive_pipe = Listener(PIPE_NAME)
        except PermissionError:
            try:
                Client(PIPE_NAME).close()
            except Exception:
                traceback.print_exc()
            sys.exit('Undo_MoM2e is already running.')

        load_settings()
        root = UndoRoot()
        app  = UndoApplication(root)
        root.config(cursor='wait')
        root.update()

        if not SAVEGAME_DIR.is_dir():
            answered_yes = messagebox.askyesno("Can't find SaveGame folder",
               f"Undo can't find the {GAME_NAME_TEXT} SaveGame folder. This is usually "
               f"because {GAME_NAME_TEXT} has never been started before on this computer. "
                "Would you like to start it now? (If you choose No, Undo will exit.)",
                icon=messagebox.QUESTION, default=messagebox.YES)
            if answered_yes:
                app.handle_open_mom_clicked()
                WaitForDirDialog(root, SAVEGAME_DIR)  # wait for the directory to be created
            if not SAVEGAME_DIR.is_dir():  # if it's still not there, then the user must have chosen to exit
                root.destroy()
                sys.exit(f"Can't find the {GAME_NAME_TEXT} SaveGame folder.")

        MYDATA_DIR.mkdir(exist_ok=True)

        cur_savegame_is_known = load_undo_states()
        if not cur_savegame_is_known:
            handle_new_savegame()

        root.bind('<<new_savegame>>',  handle_new_savegame)
        root.bind('<<watcher_error>>', handle_watcher_error)
        watcher_thread = threading.Thread(target=watch_directory, args=(SAVEGAME_DIR, send_new_savegame_event), daemon=True)
        watcher_thread.start()

        restore_window_thread = threading.Thread(target=restore_window_listener, args=(exclusive_pipe,), daemon=True)
        restore_window_thread.start()

        root.config(cursor='')
        root.mainloop()

    finally:
        if exclusive_pipe:
            exclusive_pipe.close()

# A dialog box which waits for a directory to be created, or for the user to click Exit
class WaitForDirDialog(simpledialog.Dialog):
    def __init__(self, parent, directory):
        self.directory = directory
        super().__init__(parent, 'Waiting ...')
    def body(self, master):
        self.resizable(tk.FALSE, tk.FALSE)
        ttk.Label(self, text=f'Waiting for {GAME_NAME_TEXT} to finish starting ...').pack(pady=8)
        progress_bar = ttk.Progressbar(self, length=350, mode='indeterminate')
        progress_bar.pack(padx=16)
        progress_bar.start()
        self.after(1000, self.check_dir)
    def buttonbox(self):
        exit_button = ttk.Button(self, text='Exit', command=self.cancel, underline=1)
        exit_button.pack(pady=8)
        self.bind('<Alt_L><x>', lambda e: exit_button.invoke())
        self.bind('<Alt_R><x>', lambda e: exit_button.invoke())
    def check_dir(self):
        if self.directory.is_dir():
            self.cancel()
        self.after(1000, self.check_dir)

if __name__ == '__main__':
    try:
        main()
    except Exception:
        msg = ''.join(traceback.format_exc())
        messagebox.showerror('Exception', msg)
        if root:
            root.destroy()
        sys.exit(msg)
