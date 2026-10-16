"""Navigation reuse and cache invalidation, with real Qt and offline fixtures."""
from unittest.mock import patch
from test_model_providers import ProviderUI


class TransitionCacheTests(ProviderUI):
    def test_repeated_navigation_reuses_both_backgrounds(self):
        window=self.window
        with patch.object(window.assets,'paint_static',wraps=window.assets.paint_static) as paint:
            window._scene_cache_signature=None
            window.set_screen('home'); home=window.scene_cache()
            window.set_screen('call'); call=window.scene_cache()
            self.assertEqual(paint.call_count,2)
            for _ in range(8):
                window.set_screen('home'); self.assertIs(window.scene_cache(),home)
                window.set_screen('call'); self.assertIs(window.scene_cache(),call)
            self.assertEqual(paint.call_count,2); self.assertEqual(len(window._scene_variants),2)

    def test_resize_and_portrait_changes_release_old_variants(self):
        window=self.window; window.set_screen('home'); old=window.scene_cache()
        window.set_screen('call'); window.scene_cache()
        window.resize(960,602); self.app.processEvents(); resized=window.scene_cache()
        self.assertIsNot(resized,old); self.assertEqual(resized.width(),round(960*window.devicePixelRatioF()))
        self.assertLessEqual(len(window._scene_variants),2)
        self.seed(100); changed=window.scene_cache()
        self.assertIsNot(changed,resized); self.assertLessEqual(len(window._scene_variants),2)

    def test_cached_pixels_match_independent_rendering(self):
        from PySide6.QtGui import QColor,QPainter,QPixmap
        from PySide6.QtCore import Qt
        window=self.window
        for screen in ('home','call','home','call'):
            window.set_screen(screen); cache=window.scene_cache()
            expected=QPixmap(cache.size()); expected.setDevicePixelRatio(cache.devicePixelRatio())
            expected.fill(QColor(window.colors['background']))
            painter=QPainter(expected); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.translate(window.offset_x,window.offset_y); painter.scale(window.scale,window.scale)
            window.assets.paint_static(painter,phone=screen=='call'); painter.end()
            self.assertEqual(cache.toImage(),expected.toImage())
