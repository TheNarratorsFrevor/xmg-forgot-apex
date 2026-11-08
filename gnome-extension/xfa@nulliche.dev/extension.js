// XMG Forgot Apex: Quick Settings tile for fans and keyboard backlight.
// Talks to xfad over the system bus (dev.nulliche.xfa).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Slider} from 'resource:///org/gnome/shell/ui/slider.js';

const BUS_NAME = 'dev.nulliche.xfa';
const OBJECT_PATH = '/dev/nulliche/xfa';

const FAN_XML = `
<node>
  <interface name="dev.nulliche.xfa.Fan">
    <property name="Mode" type="s" access="read"/>
    <property name="Source" type="s" access="read"/>
    <property name="Curve" type="a(ii)" access="read"/>
    <property name="ManualSpeed" type="ai" access="read"/>
    <property name="Temps" type="ai" access="read"/>
    <property name="Duty" type="ai" access="read"/>
    <property name="CurveTarget" type="i" access="read"/>
    <method name="SetMode"><arg name="mode" type="s" direction="in"/></method>
    <method name="SetManual"><arg name="fan0" type="i" direction="in"/><arg name="fan1" type="i" direction="in"/></method>
    <method name="SetCurve"><arg name="points" type="a(ii)" direction="in"/></method>
    <method name="SetSource"><arg name="source" type="s" direction="in"/></method>
  </interface>
</node>`;

const KBD_XML = `
<node>
  <interface name="dev.nulliche.xfa.Keyboard">
    <property name="Available" type="b" access="read"/>
    <property name="LedCount" type="i" access="read"/>
    <property name="Brightness" type="i" access="read"/>
    <property name="MaxBrightness" type="i" access="read"/>
    <property name="Color" type="ai" access="read"/>
    <method name="SetBrightness"><arg name="value" type="i" direction="in"/></method>
    <method name="SetColor"><arg name="r" type="i" direction="in"/><arg name="g" type="i" direction="in"/><arg name="b" type="i" direction="in"/></method>
  </interface>
</node>`;

const FanProxy = Gio.DBusProxy.makeProxyWrapper(FAN_XML);
const KbdProxy = Gio.DBusProxy.makeProxyWrapper(KBD_XML);

const COLOR_PRESETS = [
    ['White', [255, 255, 255]],
    ['Red', [255, 0, 0]],
    ['Orange', [255, 96, 0]],
    ['Green', [0, 255, 0]],
    ['Cyan', [0, 255, 255]],
    ['Blue', [0, 64, 255]],
    ['Purple', [160, 0, 255]],
    ['Pink', [255, 0, 128]],
];

function unpackAi(variant) {
    return variant ? variant.deepUnpack() : [];
}

// A menu row with a label on the left and a slider on the right.
const SliderItem = GObject.registerClass(
class SliderItem extends PopupMenu.PopupBaseMenuItem {
    _init(label) {
        super._init({activate: false});
        this._label = new St.Label({text: label, y_align: Clutter.ActorAlign.CENTER});
        this.add_child(this._label);
        this.slider = new Slider(0);
        this.slider.accessible_name = label;
        this.slider.x_expand = true;
        this.add_child(this.slider);
        this._value = new St.Label({y_align: Clutter.ActorAlign.CENTER, style_class: 'xfa-slider-value'});
        this.add_child(this._value);
    }

    setValueText(text) {
        this._value.text = text;
    }
});

const XfaToggle = GObject.registerClass(
class XfaToggle extends QuickSettings.QuickMenuToggle {
    _init() {
        super._init({
            title: 'Fans',
            subtitle: 'connecting…',
            iconName: 'weather-windy-symbolic',
            toggleMode: false,
        });
        this._fan = null;
        this._kbd = null;
        this._updating = false;

        this.menu.setHeader('weather-windy-symbolic', 'XMG Forgot Apex', 'xfad not running');

        // --- fan mode section
        this._modeSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._modeSection);
        this._modeItems = {};
        for (const [mode, label] of [['auto', 'Automatic (EC curve)'], ['curve', 'Custom curve'], ['manual', 'Manual speed']]) {
            const item = new PopupMenu.PopupMenuItem(label);
            item.connect('activate', () => this._setMode(mode));
            this._modeSection.addMenuItem(item);
            this._modeItems[mode] = item;
        }
        this._manualItem = new SliderItem('Speed');
        this._manualItem.slider.connect('notify::value', () => this._onManualSlider());
        this._modeSection.addMenuItem(this._manualItem);

        // --- keyboard section
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Keyboard backlight'));
        this._kbdSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._kbdSection);
        this._brightnessItem = new SliderItem('Brightness');
        this._brightnessItem.slider.connect('notify::value', () => this._onBrightnessSlider());
        this._kbdSection.addMenuItem(this._brightnessItem);

        const swatchRow = new PopupMenu.PopupBaseMenuItem({activate: false, reactive: false});
        const box = new St.BoxLayout({style_class: 'xfa-swatches', x_expand: true});
        swatchRow.add_child(box);
        for (const [name, rgb] of COLOR_PRESETS) {
            const btn = new St.Button({
                style_class: 'xfa-swatch',
                style: `background-color: rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]});`,
                accessible_name: name,
                can_focus: true,
            });
            btn.connect('clicked', () => this._setColor(rgb));
            box.add_child(btn);
        }
        this._kbdSection.addMenuItem(swatchRow);
        this._kbdUnavailable = new PopupMenu.PopupMenuItem('No keyboard backlight device', {reactive: false});
        this._kbdSection.addMenuItem(this._kbdUnavailable);

        // Toggle click: auto <-> custom curve
        this.connect('clicked', () => {
            if (!this._fan)
                return;
            const mode = this._fan.Mode === 'auto' ? 'curve' : 'auto';
            this._setMode(mode);
        });

        this._connectProxies();
    }

    _connectProxies() {
        new FanProxy(Gio.DBus.system, BUS_NAME, OBJECT_PATH, (proxy, error) => {
            if (error) {
                console.error(`xfa: fan proxy: ${error.message}`);
                return;
            }
            this._fan = proxy;
            this._fan.connect('g-properties-changed', () => this._refreshFan());
            this._fan.connect('notify::g-name-owner', () => this._refreshFan());
            this._refreshFan();
        });
        new KbdProxy(Gio.DBus.system, BUS_NAME, OBJECT_PATH, (proxy, error) => {
            if (error) {
                console.error(`xfa: keyboard proxy: ${error.message}`);
                return;
            }
            this._kbd = proxy;
            this._kbd.connect('g-properties-changed', () => this._refreshKbd());
            this._refreshKbd();
        });
    }

    _refreshFan() {
        if (!this._fan)
            return;
        const alive = this._fan.g_name_owner !== null;
        if (!alive) {
            this.subtitle = 'xfad not running';
            this.checked = false;
            this.menu.setHeader('weather-windy-symbolic', 'XMG Forgot Apex', 'xfad not running');
            return;
        }
        const temps = unpackAi(this._fan.get_cached_property('Temps'));
        const duty = unpackAi(this._fan.get_cached_property('Duty'));
        const manual = unpackAi(this._fan.get_cached_property('ManualSpeed'));
        const mode = this._fan.Mode ?? 'auto';
        const target = this._fan.CurveTarget ?? -1;

        const modeLabel = {auto: 'Auto', curve: 'Curve', manual: 'Manual'}[mode] ?? mode;
        this.subtitle = `${modeLabel} · ${duty[0] ?? '?'}% / ${duty[1] ?? '?'}%`;
        this.checked = mode !== 'auto';
        let header = `CPU ${temps[0] ?? '?'}°  GPU ${temps[1] ?? '?'}°  ·  fans ${duty[0] ?? '?'}% / ${duty[1] ?? '?'}%`;
        if (mode === 'curve' && target >= 0)
            header += `  ·  target ${target}%`;
        this.menu.setHeader('weather-windy-symbolic', 'XMG Forgot Apex', header);

        for (const [m, item] of Object.entries(this._modeItems))
            item.setOrnament(m === mode ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);

        this._manualItem.visible = mode === 'manual';
        if (mode === 'manual' && manual.length === 2) {
            this._updating = true;
            this._manualItem.slider.value = manual[0] / 100;
            this._manualItem.setValueText(`${manual[0]}%`);
            this._updating = false;
        }
    }

    _refreshKbd() {
        if (!this._kbd)
            return;
        const available = this._kbd.Available === true;
        this._brightnessItem.visible = available;
        this._kbdUnavailable.visible = !available;
        const swatchRow = this._kbdSection._getMenuItems()[1];
        if (swatchRow)
            swatchRow.visible = available;
        if (!available)
            return;
        const max = this._kbd.MaxBrightness || 1;
        const cur = this._kbd.Brightness ?? 0;
        this._updating = true;
        this._brightnessItem.slider.value = cur / max;
        this._brightnessItem.setValueText(`${Math.round(cur * 100 / max)}%`);
        this._updating = false;
    }

    _setMode(mode) {
        if (!this._fan)
            return;
        this._fan.SetModeRemote(mode, (res, err) => {
            if (err)
                console.error(`xfa: SetMode: ${err.message}`);
        });
    }

    _onManualSlider() {
        if (this._updating || !this._fan)
            return;
        const pct = Math.round(this._manualItem.slider.value * 100);
        this._manualItem.setValueText(`${pct}%`);
        // debounce: send after the user stops dragging for 150 ms
        if (this._manualTimer)
            GLib.source_remove(this._manualTimer);
        this._manualTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
            this._manualTimer = 0;
            this._fan.SetManualRemote(pct, pct, (res, err) => {
                if (err)
                    console.error(`xfa: SetManual: ${err.message}`);
            });
            return GLib.SOURCE_REMOVE;
        });
    }

    _onBrightnessSlider() {
        if (this._updating || !this._kbd)
            return;
        const max = this._kbd.MaxBrightness || 1;
        const value = Math.round(this._brightnessItem.slider.value * max);
        this._brightnessItem.setValueText(`${Math.round(value * 100 / max)}%`);
        if (this._brightnessTimer)
            GLib.source_remove(this._brightnessTimer);
        this._brightnessTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
            this._brightnessTimer = 0;
            this._kbd.SetBrightnessRemote(value, (res, err) => {
                if (err)
                    console.error(`xfa: SetBrightness: ${err.message}`);
            });
            return GLib.SOURCE_REMOVE;
        });
    }

    _setColor(rgb) {
        if (!this._kbd)
            return;
        this._kbd.SetColorRemote(rgb[0], rgb[1], rgb[2], (res, err) => {
            if (err)
                console.error(`xfa: SetColor: ${err.message}`);
        });
    }

    destroy() {
        if (this._manualTimer)
            GLib.source_remove(this._manualTimer);
        if (this._brightnessTimer)
            GLib.source_remove(this._brightnessTimer);
        this._manualTimer = 0;
        this._brightnessTimer = 0;
        this._fan = null;
        this._kbd = null;
        super.destroy();
    }
});

const XfaIndicator = GObject.registerClass(
class XfaIndicator extends QuickSettings.SystemIndicator {
    _init() {
        super._init();
        this._toggle = new XfaToggle();
        this.quickSettingsItems.push(this._toggle);
    }
});

export default class XfaExtension extends Extension {
    enable() {
        this._indicator = new XfaIndicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator.quickSettingsItems.forEach(item => item.destroy());
        this._indicator.destroy();
        this._indicator = null;
    }
}
