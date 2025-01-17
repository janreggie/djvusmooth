# encoding=UTF-8

# Copyright © 2008-2014 Jakub Wilk <jwilk@jwilk.net>
#
# This file is part of djvusmooth.
#
# djvusmooth is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published
# by the Free Software Foundation.
#
# djvusmooth is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.

import wx

class ProgressDialog(wx.ProgressDialog):

    def __init__(self, title, message, maximum=100, parent=None, style=(wx.PD_AUTO_HIDE | wx.PD_APP_MODAL)):
        wx.ProgressDialog.__init__(self, title, message, maximum, parent, style)
        self.__max = maximum
        self.__n = 0

    try:
        wx.ProgressDialog.Pulse
    except AttributeError:
        def Pulse(self):
            self.__n = (self.__n + 1) % self.__max
            self.Update(self.__n)

try:
    NumberEntryDialog = wx.NumberEntryDialog
except AttributeError:
    class NumberEntryDialog(wx.SingleChoiceDialog):
        def __init__(self, parent, message, prompt, caption, value, min, max, pos=wx.DefaultPosition):
            wx.SingleChoiceDialog.__init__(self, parent=parent, message=message, caption=caption, choices=list(map(str, range(min, max + 1))), pos=pos)
            self.SetSelection(value - min)

        def GetValue(self):
            return int(wx.SingleChoiceDialog.GetStringSelection(self))

__all__ = ['ProgressDialog', 'NumberEntryDialog']

# vim:ts=4 sts=4 sw=4 et
