# encoding=UTF-8
# Copyright © 2008 Jakub Wilk <ubanus@users.sf.net>

import re
import wx

import djvu.sexpr

import models.outline
from varietes import replace_control_characters

def get_label_for_node(node):
	return replace_control_characters(' ', node.text)

class OutlineCallback(models.outline.OutlineCallback):

	def __init__(self, browser):
		self._browser = browser
	
	def notify_node_change(self, node):
		wx.CallAfter(lambda: self._browser.on_node_change(node))
	
	def notify_node_select(self, node):
		wx.CallAfter(lambda: self._browser.on_node_select(node))

	def notify_node_children_change(self, node):
		# FIXME: consider something lighter here
		wx.CallAfter(lambda: self._browser.on_tree_change(node))

	def notify_tree_change(self, node):
		wx.CallAfter(lambda: self._browser.on_tree_change(node))
	
class OutlineBrowser(wx.TreeCtrl):

	def __init__(self, parent, id = wx.ID_ANY, pos = wx.DefaultPosition, size = wx.DefaultSize, style = wx.TR_HAS_BUTTONS | wx.TR_EDIT_LABELS):
		wx.TreeCtrl.__init__(self, parent, id, pos, size, style)
		self._have_root = False
		self._document = None
		self.Bind(wx.EVT_TREE_BEGIN_LABEL_EDIT, self.on_begin_edit, self)
		self.Bind(wx.EVT_TREE_END_LABEL_EDIT, self.on_end_edit, self)
		self.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_selection_changed, self)
		self.Bind(wx.EVT_CHAR, self.on_char)
		self.Bind(wx.EVT_TREE_BEGIN_DRAG, self.on_begin_drag)
		self.Bind(wx.EVT_TREE_END_DRAG, self.on_end_drag)
	
	def on_begin_drag(self, event):
		drag_item = event.GetItem()
		if drag_item == self._root_item:
			event.Veto()
			return
		self._drag_item = drag_item
		node = self.GetPyData(drag_item)
		if node is not None:
			event.Allow()
	
	def on_end_drag(self, event):
		source = self._drag_item
		del self._drag_item
		target = event.GetItem()
		if not target.IsOk():
			return
		if source == target:
			return
		source_node = self.GetPyData(source)
		if source_node is None:
			return
		target_node = self.GetPyData(target)
		if target_node is None:
			return

		target_ancestor = target_node
		while True:
			if target_ancestor is source_node:
				# Cannot move a node to its child
				return 
			try:
				target_ancestor = target_ancestor.parent
			except StopIteration:
				break
		source_node.delete()
		target_node.add_child(source_node)
		source_node.notify_select()

	def do_goto_node(self, node):
		uri = node.uri
		if uri.startswith('#'):
			try:
				n = int(buffer(uri, 1))
			except ValueError:
				return # TODO: try to handle non-local URIs
			parent = wx.GetTopLevelParent(self)
			parent.page_no = n - 1
		else:
			return # TODO: try to handle non-local URIs
	
	def do_delete_node(self, node):
		node.delete()

	_WXK_TO_METHOD = {
		wx.WXK_RETURN: do_goto_node,
		wx.WXK_DELETE: do_delete_node
	}

	def on_char(self, event):
		key_code = event.GetKeyCode()
		try:
			method = self._WXK_TO_METHOD[key_code]
		except KeyError:
			return
		item = self.GetSelection()
		node = self.GetPyData(item)
		if node is None:
			return
		method(self, node)

	def on_node_select(self, node):
		try:
			item = self._items[node]
		except KeyError:
			return
		if self.GetSelection() != item:
			self.SelectItem(item)

	def on_node_change(self, node):
		try:
			item = self._items[node]
		except KeyError:
			return
		self.SetItemText(item, get_label_for_node(node))

	def on_tree_change(self, model_node):
		self.document = True

	@apply
	def document():
		def get(self):
			return self._document
		def set(self, value):
			if value is not True:
				self._document = value
			if self._document is not None:
				self._callback = OutlineCallback(self)
				self._document.outline.register_callback(self._callback)
			self._recreate_children()
		return property(get, set)

	def on_selection_changed(self, event):
		event.Skip()
		item = event.GetItem()
		if not item:
			return
		node = self.GetPyData(item)
		if node is None:
			return
		node.notify_select()

	def on_begin_edit(self, event):
		item = event.GetItem()
		if not self.do_begin_edit(item):
			event.Veto()
	
	def do_begin_edit(self, item):
		if item == self._root_item:
			return
		node = self.GetPyData(item)
		if node is None:
			return
		self.SetItemText(item, node.text)
		return True
	
	def on_end_edit(self, event):
		item = event.GetItem()
		if event.IsEditCancelled():
			new_text = None
		else:
			new_text = event.GetLabel()
		if not self.do_end_edit(item, new_text):
			event.Veto()
	
	def do_end_edit(self, item, text):
		node = self.GetPyData(item)
		if node is None:
			return
		if text is None:
			text = node.text
		node.text = text
		return True

	def _add_children(self, item, nodes):
		for node in nodes:
			symbol = node.type
			label = get_label_for_node(node)
			child_item = self.AppendItem(item, label)
			self._add_children(child_item, node)
			self._items[node] = child_item
			self.SetPyData(child_item, node)

	def _recreate_children(self):
		self._items = {}
		root = self.GetRootItem()
		if root.IsOk():
			self.Delete(root)
		if self.document is None:
			return
		node = self.document.outline.root
		if node:
			self._root_item = self.AddRoot(str(node.type))
			self.SetPyData(self._root_item, node)
			self._have_root = True
			self._add_children(self._root_item, node)

__all__ = 'OutlineBrowser',

# vim:ts=4 sw=4