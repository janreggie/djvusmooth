# encoding=UTF-8
# Copyright © 2008 Jakub Wilk <ubanus@users.sf.net>

APPLICATION_NAME = 'DjVuSmooth'
LICENSE = 'GPL-2'
__version__ = '0.1'
__author__ = 'Jakub Wilk <ubanus@users.sf.net>'

import itertools
import os.path
import threading
import tempfile
from Queue import Queue, Empty as QueueEmpty

import dependencies as __dependencies

import wx
import wx.lib.ogl
import wx.lib.newevent
import wx.lib.scrolledpanel

import djvu.decode
import djvu.const

from djvused import StreamEditor
from gui.page import PageWidget, PercentZoom, OneToOneZoom, StretchZoom, FitWidthZoom, FitPageZoom
from gui.page import RENDER_NONRASTER_TEXT, RENDER_NONRASTER_MAPAREA
from gui.metadata import MetadataDialog
from gui.flatten_text import FlattenTextDialog
from gui.text_browser import TextBrowser
from gui.outline_browser import OutlineBrowser
from gui.maparea_browser import MapAreaBrowser
import text.mangle as text_mangle
import gui.dialogs
import models
import models.metadata
import models.annotations
import models.text
import external_editor

MENU_ICON_SIZE = (16, 16)
DJVU_WILDCARD = 'DjVu files (*.djvu, *.djv)|*.djvu;*.djv|All files|*'

WxDjVuMessage, wx.EVT_DJVU_MESSAGE = wx.lib.newevent.NewEvent()

class OpenDialog(wx.FileDialog):

	def __init__(self, parent):
		wx.FileDialog.__init__(self, parent, style = wx.OPEN, wildcard=DJVU_WILDCARD)

class TextModel(models.text.Text):
	def __init__(self, document):
		models.text.Text.__init__(self)
		self._document = document
	
	def acquire_data(self, n):
		text = self._document.pages[n].text
		text.wait()
		return text.sexpr

class OutlineModel(models.outline.Outline):

	def __init__(self, document):
		self._document = document
		models.outline.Outline.__init__(self)
	
	def acquire_data(self):
		outline = self._document.outline
		outline.wait()
		return outline.sexpr

class PageProxy(object):
	def __init__(self, page, text_model, annotations_model):
		self._page = page
		self._text = text_model
		self._annotations = annotations_model
	
	@property
	def page_job(self):
		return self._page.decode(wait = False)
	
	@property
	def text(self):
		return self._text
	
	@property
	def annotations(self):
		return self._annotations

	def register_text_callback(self, callback):
		self._text.register_callback(callback)

	def register_annotations_callback(self, callback):
		self._annotations.register_callback(callback)

class DocumentProxy(object):

	def __init__(self, document, outline):
		self._document = document
		self._outline = outline
	
	@property
	def outline(self):
		return self._outline

	def register_outline_callback(self, callback):
		self._outline.register_callback(callback)

class AnnotationsModel(models.annotations.Annotations):

	def __init__(self, document_path):
		models.annotations.Annotations.__init__(self)
		self.__djvused = StreamEditor(document_path)
	
	def acquire_data(self, n):
		djvused = self.__djvused
		if n == models.SHARED_ANNOTATIONS_PAGENO:
			djvused.select_shared_annotations()
		else:
			djvused.select(n + 1)
		djvused.print_annotations()
		s = '(%s)' % djvused.commit() # FIXME: optimize
		try:
			return djvu.sexpr.Expression.from_string(s)
		except djvu.sexpr.ExpressionSyntaxError:
			raise # FIXME

class MetadataModel(models.metadata.Metadata):
	def __init__(self, document):
		models.metadata.Metadata.__init__(self)
		self._document = document

	def acquire_data(self, n):
		document_annotations = self._document.annotations
		document_annotations.wait()
		document_metadata = document_annotations.metadata
		if n == models.SHARED_ANNOTATIONS_PAGENO:
			result = document_metadata
		else:
			page_annotations = self._document.pages[n].annotations
			page_annotations.wait()
			page_metadata = page_annotations.metadata
			result = {}
			for k, v in page_metadata.iteritems():
				if k not in document_metadata:
					pass
				elif v != document_metadata[k]:
					pass
				else:
					continue
				result[k] = v
		return result

class PageTextCallback(models.text.PageTextCallback):

	def __init__(self, owner):
		self._owner = owner

	def notify_node_change(self, node):
		self._owner.dirty = True
	
	def notify_node_children_change(self, node):
		self._owner.dirty = True
	
	def notify_node_select(self, node):
		text = '[Text layer] %s' % node.type
		if node.is_leaf():
			text += ': %s' % node.text
		self._owner.SetStatusText(text)

	def notify_node_deselect(self, node):
		self._owner.SetStatusText('')
		
	def notify_tree_change(self, node):
		self._owner.dirty = True

class OutlineCallback(models.outline.OutlineCallback):

	def __init__(self, owner):
		self._owner = owner

	def notify_tree_change(self, node):
		self._owner.dirty = True

	def notify_node_change(self, node):
		self._owner.dirty = True

	def notify_node_children_change(self, node):
		self._owner.dirty = True

	def notify_node_select(self, node):
		try:
			self._owner.SetStatusText('Link: %s' % node.uri)
		except AttributeError:
			pass

class PageAnnotationsCallback(models.annotations.PageAnnotationsCallback):

	def __init__(self, owner):
		self._owner = owner
	
	def notify_node_change(self, node):
		self._owner.dirty = True
	def notify_node_add(self, node):
		self._owner.dirty = True
	def notify_node_delete(self, node):
		self._owner.dirty = True
	def notify_node_replace(self, node, other_node):
		self._owner.dirty = True

	def notify_node_select(self, node):
		try:
			self._owner.SetStatusText('Link: %s' % node.uri)
		except AttributeError:
			pass

	def notify_node_deselect(self, node):
		self._owner.SetStatusText('')

class ScrolledPanel(wx.lib.scrolledpanel.ScrolledPanel):

	def OnChildFocus(self, event):
		# We *don't* want to scroll to the child window which just got the focus.
		# So just skip the event:
		event.Skip()

class MainWindow(wx.Frame):
	
	def new_menu_item(self, menu, caption, help, method, style = wx.ITEM_NORMAL, icon = None, id = wx.ID_ANY):
		item = wx.MenuItem(menu, id, caption, help, style)
		if icon is not None:
			bitmap = wx.ArtProvider_GetBitmap(icon, wx.ART_MENU, MENU_ICON_SIZE)
			item.SetBitmap(bitmap)
		self.Bind(wx.EVT_MENU, method, item)
		menu.AppendItem(item)
		return item

	def __init__(self):
		wx.Frame.__init__(self, None, size=wx.Size(640, 480))
		self.Bind(wx.EVT_DJVU_MESSAGE, self.handle_message)
		self.context = Context(self)
		self._page_text_callback = PageTextCallback(self)
		self._page_annotations_callback = PageAnnotationsCallback(self)
		self._outline_callback = OutlineCallback(self)
		self.status_bar = self.CreateStatusBar(2, style = wx.ST_SIZEGRIP)
		self.splitter = wx.SplitterWindow(self, style = wx.SP_LIVE_UPDATE)
		self.sidebar = wx.Choicebook(self.splitter, wx.ID_ANY)
		self.text_browser = TextBrowser(self.sidebar)
		self.outline_browser = OutlineBrowser(self.sidebar)
		self.maparea_browser = MapAreaBrowser(self.sidebar)
		self.sidebar.AddPage(self.outline_browser, 'Outline')
		self.sidebar.AddPage(self.maparea_browser, 'Hyperlinks')
		self.sidebar.AddPage(self.text_browser, 'Text')
		self.sidebar.Bind(
			wx.EVT_CHOICEBOOK_PAGE_CHANGED,
			self._on_sidebar_page_changed(
				self.on_display_no_nonraster,
				self.on_display_maparea,
				self.on_display_text)
		)
		sidebar_sizer = wx.BoxSizer(wx.VERTICAL)
		self.sidebar.SetSizer(sidebar_sizer)
		sidebar_sizer.Add(self.text_browser, 1, wx.ALL | wx.EXPAND)
		self.scrolled_panel = ScrolledPanel(self.splitter)
		self.splitter._default_position = 160
		self.splitter.SetSashGravity(0.1)
		self.do_show_sidebar()
		sizer = wx.BoxSizer(wx.VERTICAL)
		self.scrolled_panel.SetSizer(sizer)
		self.scrolled_panel.SetupScrolling()
		self.page_widget = PageWidget(self.scrolled_panel)
		self.page_widget.Bind(wx.EVT_CHAR, self.on_char)
		self.scrolled_panel.Bind(wx.EVT_SIZE, self.page_widget.on_parent_resize)
		sizer.Add(self.page_widget, 0, wx.ALL, 0)
		self.editable_menu_items = []
		self.saveable_menu_items = []
		menu_bar = wx.MenuBar()
		menu = wx.Menu()
		self.new_menu_item(menu, '&Open\tCtrl+O', 'Open a DjVu document', self.on_open, icon=wx.ART_FILE_OPEN)
		save_menu_item = self.new_menu_item(menu, '&Save\tCtrl+S', 'Save the document', self.on_save, icon=wx.ART_FILE_SAVE)
		close_menu_item = self.new_menu_item(menu, '&Close\tCtrl+W', 'Close the document', self.on_close, id=wx.ID_CLOSE)
		self.editable_menu_items += close_menu_item,
		self.saveable_menu_items += save_menu_item,
		menu.AppendSeparator()
		self.new_menu_item(menu, '&Quit\tCtrl+Q', 'Quit the application', self.on_exit, icon=wx.ART_QUIT)
		menu_bar.Append(menu, '&File');
		menu = wx.Menu()
		self.new_menu_item(menu, '&Metadata\tCtrl+M', 'Edit the document or page metadata', self.on_edit_metadata)
		submenu = wx.Menu()
		self.new_menu_item(submenu, '&External editor\tCtrl+T', 'Edit page text in an external editor', self.on_external_edit_text)
		self.new_menu_item(submenu, '&Flatten', 'Remove details from page text', self.on_flatten_text)
		menu.AppendMenu(wx.ID_ANY, '&Text', submenu)
		submenu = wx.Menu()
		self.new_menu_item(submenu, '&Bookmark this page\tCtrl+B', 'Add the current to document outline', self.on_bookmark_current_page)
		self.new_menu_item(submenu, '&External editor', 'Edit document outline in an external editor', self.on_external_edit_outline)
		self.new_menu_item(submenu, '&Remove', 'Remove document outline', self.on_remove_outline)
		menu.AppendMenu(wx.ID_ANY, '&Outline', submenu)
		menu_bar.Append(menu, '&Edit');
		menu = wx.Menu()
		submenu = wx.Menu()
		for caption, help, method, id in \
		[
			('Zoom &in',  'Increase the magnification', self.on_zoom_in, wx.ID_ZOOM_IN),
			('Zoom &out', 'Decrease the magnification', self.on_zoom_out, wx.ID_ZOOM_OUT),
		]:
			self.new_menu_item(submenu, caption, help, method, id = id or wx.ID_ANY)
		submenu.AppendSeparator()
		for caption, help, zoom, id in \
		[
			('Fit &width',  'Set magnification to fit page width',  FitWidthZoom(), None),
			('Fit &page',   'Set magnification to fit page',        FitPageZoom(),  wx.ID_ZOOM_FIT),
			('&Stretch',    'Stretch the image to the window size', StretchZoom(),  None),
			('One &to one', 'Set full resolution magnification.',  OneToOneZoom(),  wx.ID_ZOOM_100),
		]:
			self.new_menu_item(submenu, caption, help, self.on_zoom(zoom), style=wx.ITEM_RADIO, id = id or wx.ID_ANY)
		submenu.AppendSeparator()
		self.zoom_menu_items = {}
		for percent in 300, 200, 150, 100, 75, 50, 25:
			item = self.new_menu_item(
				submenu,
				'%d%%' % percent,
				'Magnify %d%%' % percent,
				self.on_zoom(PercentZoom(percent)),
				style=wx.ITEM_RADIO
			)
			if percent == 100:
				item.Check()
			self.zoom_menu_items[percent] = item
		menu.AppendMenu(wx.ID_ANY, '&Zoom', submenu)

		submenu = wx.Menu()
		for caption, help, method in \
		[
			('&Color\tAlt+C', 'Display everything',                                            self.on_display_everything),
			('&Stencil',      'Display only the document bitonal stencil',                     self.on_display_stencil),
			('&Foreground',   'Display only the foreground layer',                             self.on_display_foreground),
			('&Background',   'Display only the foreground layer',                             self.on_display_background),
			('&None\tAlt+N',  'Neither display the foreground layer nor the background layer', self.on_display_none)
		]:
			self.new_menu_item(submenu, caption, help, method, style=wx.ITEM_RADIO)
		menu.AppendMenu(wx.ID_ANY, '&Image', submenu)
		submenu = wx.Menu()
		_checked = False
		for caption, help, method in \
		[
			('&None',              u'Don\'t display non-raster data',  self.on_display_no_nonraster),
			('&Hyperlinks\tAlt+H', u'Display overprinted annotations', self.on_display_maparea),
			('&Text\tAlt+T',       u'Display the text layer',          self.on_display_text),
		]:
			if not _checked:
				item.Check()
				_checked = True
			item = self.new_menu_item(submenu, caption, help, method, style=wx.ITEM_RADIO)
		del _checked

		menu.AppendMenu(wx.ID_ANY, '&Non-raster data', submenu)
		self.new_menu_item(menu, '&Refresh\tCtrl+L', 'Refresh the window', self.on_refresh)
		menu_bar.Append(menu, '&View')
		menu = wx.Menu()
		for caption, help, method, icon in \
		[
			('&First page\tCtrl-Home', u'first document page',    self.on_first_page,    None),
			('&Previous page\tPgUp',   u'previous document page', self.on_previous_page, wx.ART_GO_UP),
			('&Next page\tPgDn',       u'next document page',     self.on_next_page,     wx.ART_GO_DOWN),
			('&Last page\tCtrl-End',   u'last document page',     self.on_last_page,     None),
			(u'&Go to page…',          u'page…',                  self.on_goto_page,     None)
		]:
			self.new_menu_item(menu, 'Jump to ' + caption, help, method, icon = icon)
		menu_bar.Append(menu, '&Go');
		menu = wx.Menu()
		self.new_menu_item(menu, 'Show &sidebar\tF9', 'Show/side the sidebar', self.on_show_sidebar, style=wx.ITEM_CHECK).Check()
		menu_bar.Append(menu, '&Settings');
		menu = wx.Menu()
		self.new_menu_item(menu, '&About\tF1', 'More information about this program', self.on_about, id=wx.ID_ABOUT)
		menu_bar.Append(menu, '&Help');
		self.SetMenuBar(menu_bar)
		self.dirty = False
		self.do_open(None)
		self.Bind(wx.EVT_CLOSE, self.on_exit)
	
	def _on_sidebar_page_changed(self, *methods):
		def event_handler(event):
			methods[event.GetSelection()](event)
		return event_handler

	def on_char(self, event):
		key_code = event.GetKeyCode()
		if key_code == ord('-'):
			self.on_zoom_out(event)
		elif key_code == ord('+'):
			self.on_zoom_in(event)
		else:
			event.Skip()

	def enable_edit(self, enable=True):
		for i in 1, 3:
			self.GetMenuBar().EnableTop(i, enable)
		for menu_item in self.editable_menu_items:
			menu_item.Enable(enable)

	def enable_save(self, enable=True):
		for menu_item in self.saveable_menu_items:
			menu_item.Enable(enable)

	@apply
	def dirty():
		def get(self):
			return self._dirty
		def set(self, value):
			self._dirty = value
			self.enable_save(value)
		return property(get, set)

	def error_box(self, message, caption = 'Error'):
		wx.MessageBox(message = message, caption = caption, style = wx.OK | wx.ICON_ERROR, parent = self)

	def on_exit(self, event):
		if self.do_open(None):
			self.Destroy()
	
	def on_open(self, event):
		dialog = OpenDialog(self)
		try:
			if dialog.ShowModal() == wx.ID_OK:
				self.do_open(dialog.GetPath())
		finally:
			dialog.Destroy()

	def on_close(self, event):
		self.do_open(None)

	def on_save(self, event):
		self.do_save()
	
	def do_save(self):
		if not self.dirty:
			return
		queue = Queue()
		sed = StreamEditor(self.path, autosave=True)
		for model in self.models:
			model.export(sed)
		def job():
			try:
				sed.commit()
			except Exception, exception:
				pass
			else:
				exception = None
			queue.put(exception)
		thread = threading.Thread(target = job)
		thread.start()
		dialog = None
		try:
			try:
				queue.get(block = True, timeout = 0.1)
				return
			except QueueEmpty:
				dialog = gui.dialogs.ProgressDialog(
					title = 'Saving document',
					message = u'Saving the document, please wait…',
					parent = self,
					style = wx.PD_APP_MODAL | wx.PD_ELAPSED_TIME
				)
			while True:
				try:
					exception = queue.get(block = True, timeout = 0.1)
					if exception is not None:
						raise exception
					break
				except QueueEmpty:
					dialog.Pulse()
		finally:
			thread.join()
			self.dirty = False
			if dialog is not None:
				dialog.Destroy()

	def on_show_sidebar(self, event):
		if event.IsChecked():
			self.do_show_sidebar()
		else:
			self.do_hide_sidebar()
	
	def do_show_sidebar(self):
		self.splitter.SplitVertically(self.sidebar, self.scrolled_panel, self.splitter._default_position)
	
	def do_hide_sidebar(self):
		self.splitter.Unsplit(self.sidebar)

	def on_display_everything(self, event):
		self.page_widget.render_mode = djvu.decode.RENDER_COLOR
	
	def on_display_foreground(self, event):
		self.page_widget.render_mode = djvu.decode.RENDER_FOREGROUND

	def on_display_background(self, event):
		self.page_widget.render_mode = djvu.decode.RENDER_BACKGROUND
	
	def on_display_stencil(self, event):
		self.page_widget.render_mode = djvu.decode.RENDER_BLACK
	
	def on_display_none(self, event):
		self.page_widget.render_mode = None
	
	def on_display_text(self, event):
		self.page_widget.render_nonraster = RENDER_NONRASTER_TEXT
	
	def on_display_maparea(self, event):
		self.page_widget.render_nonraster = RENDER_NONRASTER_MAPAREA
	
	def on_display_no_nonraster(self, event):
		self.page_widget.render_nonraster = None

	def on_refresh(self, event):
		self.Refresh()

	def get_page_uri(self, page_no = None):
		if page_no is None:
			page_no = self.page_no
		try:
			id = self.document.pages[page_no].file.id
		except djvu.decode.NotAvailable:
			id = str(page_no)
		return '#' + id

	@apply
	def page_no():
		def get(self):
			return self._page_no
		def set(self, n):
			if self.document is None:
				self._page_no = 0
				self.status_bar.SetStatusText('', 1)
				return
			if n < 0 or n >= len(self.document.pages):
				return
			self._page_no = n
			self.status_bar.SetStatusText('Page %d of %d' % ((n + 1), len(self.document.pages)), 1)
			self.update_page_widget(new_page = True)
		return property(get, set)

	def on_first_page(self, event):
		self.page_no = 0

	def on_last_page(self, event):
		self.page_no = len(self.document.pages) - 1

	def on_next_page(self, event):
		self.page_no += 1

	def on_previous_page(self, event):
		self.page_no -= 1
	
	def on_goto_page(self, event):
		dialog = gui.dialogs.NumberEntryDialog(
			parent = self,
			message = 'Go to page:',
			prompt = '',
			caption = 'Go to page',
			value = self.page_no + 1,
			min = 1,
			max = len(self.document.pages)
		)
		try:
			rc = dialog.ShowModal()
			if rc == wx.ID_OK:
				self.page_no = dialog.GetValue() - 1
		finally:
			dialog.Destroy()

	def on_edit_metadata(self, event):
		document_metadata_model = self.metadata_model[models.SHARED_ANNOTATIONS_PAGENO].clone()
		document_metadata_model.title = 'Document metadata'
		page_metadata_model = self.metadata_model[self.page_no].clone()
		page_metadata_model.title = 'Page %d metadata' % (self.page_no + 1)
		dialog = MetadataDialog(self, models=(document_metadata_model, page_metadata_model), known_keys=djvu.const.METADATA_KEYS)
		try:
			if dialog.ShowModal() == wx.ID_OK:
				self.metadata_model[models.SHARED_ANNOTATIONS_PAGENO] = document_metadata_model
				self.metadata_model[self.page_no] = page_metadata_model
				self.dirty = True
		finally:
			dialog.Destroy()

	def on_flatten_text(self, event):
		dialog = FlattenTextDialog(self)
		zone = None
		try:
			if dialog.ShowModal() == wx.ID_OK:
				scope_all = dialog.get_scope()
				zone = dialog.get_zone()
		finally:
			dialog.Destroy()
		if zone is None:
			return
		if scope_all:
			page_nos = xrange(len(self.document.pages))
		else:
			page_nos = (self.page_no,)
		for page_no in page_nos:
			self.text_model[page_no].strip(zone)

	def on_bookmark_current_page(self, event):
		uri = self.get_page_uri()
		node = models.outline.InnerNode(djvu.sexpr.Expression(('(no title)', uri)), self.outline_model)
		self.outline_model.root.add_child(node)

	def on_remove_outline(self, event):
		self.outline_model.remove()

	def on_external_edit_outline(self, event):
		model = self.outline_model
		def job():
			new_repr = None
			try:
				tmp_file = tempfile.NamedTemporaryFile()
				try:
					model.export_as_plaintext(tmp_file)
					tmp_file.flush()
					external_editor.edit(tmp_file.name)
					tmp_file.seek(0)
					new_repr = map(str.expandtabs, itertools.imap(str.rstrip, tmp_file))
				finally:
					tmp_file.close()
			except Exception, exception:
				pass
			else:
				exception = None
			wx.CallAfter(lambda: self.after_external_edit_outline(new_repr, disabler, exception))
		disabler = wx.WindowDisabler()
		thread = threading.Thread(target = job)
		thread.start()

	def on_external_edit_failed(self, exception):
		self.error_box('External edit failed:\n%s' % exception)

	def after_external_edit_outline(self, new_repr, disabler, exception):
		if exception is not None:
			self.external_edit_failed(exception)
		# TODO: how to check if actually something changed?
		self.outline_model.import_plaintext(new_repr)

	def on_external_edit_text(self, event):
		sexpr = self.text_model[self.page_no].raw_value
		if not sexpr:
			self.error_box('No text layer to edit.')
			return
		def job():
			new_sexpr = None
			try:
				tmp_file = tempfile.NamedTemporaryFile()
				try:
					text_mangle.export(sexpr, tmp_file)
					tmp_file.flush()
					external_editor.edit(tmp_file.name)
					tmp_file.seek(0)
					try:
						new_sexpr = text_mangle.import_(sexpr, tmp_file)
					except text_mangle.NothingChanged:
						pass
				finally:
					tmp_file.close()
			except Exception, exception:
				pass
			else:
				exception = None
			wx.CallAfter(lambda: self.after_external_edit_text(new_sexpr, disabler, exception))
		disabler = wx.WindowDisabler()
		thread = threading.Thread(target = job)
		thread.start()
	
	def after_external_edit_text(self, sexpr, disabler, exception):
		if exception is not None:
			try:
				raise exception
			except text_mangle.CharacterZoneFound:
				self.error_box('Cannot edit text with character zones.')
				return
			except text_mangle.LengthChanged:
				self.error_box('Number of lines changed.')
				return
			except:
				self.on_external_edit_failed(exception)
		if sexpr is None:
			# nothing changed
			return
		self.text_model[self.page_no].raw_value = sexpr

	def do_percent_zoom(self, percent):
		self.page_widget.zoom = PercentZoom(percent)
		self.zoom_menu_items[percent].Check()

	def on_zoom_out(self, event):
		try:
			percent = self.page_widget.zoom.percent
		except ValueError:
			return # FIXME
		candidates = [k for k in self.zoom_menu_items.iterkeys() if k < percent]
		if not candidates:
			return
		self.do_percent_zoom(max(candidates))

	def on_zoom_in(self, event):
		try:
			percent = self.page_widget.zoom.percent
		except ValueError:
			return # FIXME
		candidates = [k for k in self.zoom_menu_items.iterkeys() if k > percent]
		if not candidates:
			return
		self.do_percent_zoom(min(candidates))

	def on_zoom(self, zoom):
		def event_handler(event):
			self.page_widget.zoom = zoom
		return event_handler
	
	def do_open(self, path):
		if self.dirty:
			dialog = wx.MessageDialog(self, 'Do you want to save your changes?', '', wx.YES_NO | wx.YES_DEFAULT | wx.CANCEL | wx.ICON_QUESTION)
			try:
				rc = dialog.ShowModal()
				if rc == wx.ID_YES:
					self.do_save()
				elif rc == wx.ID_NO:
					pass
				elif rc == wx.ID_CANCEL:
					return False
			finally:
				dialog.Destroy()
		self.path = path
		self.document = None
		self.page_no = 0
		def clear_models():
			self.metadata_model = self.text_model = self.outline_model = self.annotations_model = None
			self.models = ()
			self.enable_edit(False)
		if path is None:
			clear_models()
		else:
			try:
				self.document = self.context.new_document(djvu.decode.FileURI(path))
				self.metadata_model = MetadataModel(self.document)
				self.text_model = TextModel(self.document)
				self.outline_model = OutlineModel(self.document)
				self.annotations_model = AnnotationsModel(path)
				self.models = self.metadata_model, self.text_model, self.outline_model, self.annotations_model
				self.enable_edit(True)
			except djvu.decode.JobFailed:
				clear_models()
				self.document = None
				# Do *not* display error message here. It will be displayed by `handle_message()`.
		self.page_no = 0 # again, to set status bar text
		self.update_title()
		self.update_page_widget(new_document = True, new_page = True)
		self.dirty = False
		return True
	
	def update_page_widget(self, new_document = False, new_page = False):
		if self.document is None:
			self.page_widget.Hide()
			self.page = self.page_job = self.page_proxy = self.document_proxy = None
		elif self.page_job is None or new_page:
			self.page_widget.Show()
			self.page = self.document.pages[self.page_no]
			self.page_job = self.page.decode(wait = False)
			self.page_proxy = PageProxy(
				page = self.page,
				text_model = self.text_model[self.page_no],
				annotations_model = self.annotations_model[self.page_no]
			)
			self.page_proxy.register_text_callback(self._page_text_callback)
			self.page_proxy.register_annotations_callback(self._page_annotations_callback)
			if new_document:
				self.document_proxy = DocumentProxy(document = self.document, outline = self.outline_model)
				self.document_proxy.register_outline_callback(self._outline_callback)
		self.page_widget.page = self.page_proxy
		self.text_browser.page = self.page_proxy
		self.maparea_browser.page = self.page_proxy
		if new_document:
			self.outline_browser.document = self.document_proxy

	def update_title(self):
		if self.path is None:
			title = APPLICATION_NAME
		else:
			title = u'%s — %s' % (APPLICATION_NAME, os.path.basename(self.path))
		self.SetTitle(title)

	def on_about(self, event):
		message = '''\
%(APPLICATION_NAME)s %(__version__)s
Author: %(__author__)s
License: %(LICENSE)s''' % globals()
		wx.MessageBox(message = message, caption = u'About…')
	
	def handle_message(self, event):
		message = event.message
		# TODO: remove debug prints
		if isinstance(message, djvu.decode.ErrorMessage):
			self.error_box(message = str(message))
		elif message.document is not self.document:
			print 'IGNORED', message
		self.update_title()
		if isinstance(message, (djvu.decode.RedisplayMessage, djvu.decode.RelayoutMessage)):
			if self.page_job is message.page_job:
				self.update_page_widget()

class Context(djvu.decode.Context):

	def __new__(self, window):
		return djvu.decode.Context.__new__(self)

	def __init__(self, window):
		djvu.decode.Context.__init__(self)
		self.window = window

	def handle_message(self, message):
		wx.PostEvent(self.window, WxDjVuMessage(message=message))

class Application(wx.App):

	def __init__(self, argv):
		self._argv = argv
		wx.App.__init__(self)

	def OnInit(self):
		wx.lib.ogl.OGLInitialize()
		window = MainWindow()
		window.Show(True)
		if self._argv:
			window.do_open(self._argv.pop(0))
		return True

	def start(self):
		return self.MainLoop()

# vim:ts=4 sw=4
