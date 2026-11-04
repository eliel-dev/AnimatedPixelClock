"""Conversion limits, duration preservation, upload guards and HTTP integration."""
import base64
import io
import json
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from PIL import Image
import animation_service as service


def gif_blob(count=12, duration=100):
    frames = [Image.new('RGB', (128, 64), (i * 17 % 256, i * 31 % 256, 40)) for i in range(count)]
    out = io.BytesIO()
    frames[0].save(out, format='GIF', save_all=True, append_images=frames[1:], duration=duration, loop=0)
    return base64.b64encode(out.getvalue()).decode('ascii')


class ConversionTests(unittest.TestCase):
    def test_small_board_budget_preserves_duration(self):
        result = service.convert_gif({'gif': gif_blob(30), 'maxBytes': 98304})
        self.assertLessEqual(result['bytes'], 98304)
        self.assertEqual(result['frames'], 5)
        self.assertEqual(result['seconds'], 3)
        self.assertEqual(result['frameSkip'], 6)
        service.validate_pca(base64.b64decode(result['pca']))
        self.assertEqual(base64.b64decode(result['pca'])[:4], b'PCA2')

    def test_no_auto_fit_reports_capacity(self):
        with self.assertRaisesRegex(ValueError, 'does not fit'):
            service.convert_gif({'gif': gif_blob(30), 'maxBytes': 98304, 'autoFit': False})

    def test_one_color_still_has_valid_two_color_header(self):
        result = service.convert_gif({'gif': gif_blob(1)})
        service.validate_pca(base64.b64decode(result['pca']))

    def test_pca2_rgb565_preserves_primary_color_channels(self):
        import gif_converter as codec
        red = Image.new('RGB', (128, 64), (255, 0, 0))
        green = Image.new('RGB', (128, 64), (0, 255, 0))
        blue = Image.new('RGB', (128, 64), (0, 0, 255))
        blob = codec.build_pca2([red, green, blue], [100, 100, 100])
        service.validate_pca(blob)
        self.assertEqual(blob[:4], b'PCA2')
        offset = 12 + 3 * 2
        self.assertEqual(blob[offset:offset+2], b'\x00\xf8')
        self.assertEqual(blob[offset+128*64*2:offset+128*64*2+2], b'\xe0\x07')
        self.assertEqual(blob[offset+2*128*64*2:offset+2*128*64*2+2], b'\x1f\x00')

    def test_nearest_neighbor_preserves_source_palette_colors(self):
        import gif_converter as codec
        source = Image.new('RGB', (256, 64))
        pixels = source.load()
        for y in range(64):
            for x in range(256):
                pixels[x, y] = (255, 0, 0) if x % 2 == 0 else (0, 0, 255)

        fitted = codec.fit_frame(source, 'stretch', 'center')
        fitted_pixels = fitted.get_flattened_data() if hasattr(fitted, 'get_flattened_data') else fitted.getdata()
        self.assertEqual(set(fitted_pixels), {(255, 0, 0)})

    def test_fill_and_pad_follow_mica_hub75_pixel_mapping(self):
        import gif_converter as codec
        source = Image.new('RGB', (4, 4))
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
        source.putdata([colors[y] for y in range(4) for _ in range(4)])

        filled = codec.fit_frame(source, 'crop', 'center')
        self.assertEqual(filled.getpixel((64, 0)), colors[1])
        self.assertEqual(filled.getpixel((64, 63)), colors[2])

        padded = codec.fit_frame(source, 'pad', 'center')
        self.assertEqual(padded.getpixel((0, 32)), (0, 0, 0))
        self.assertEqual(padded.getpixel((32, 32)), colors[2])

    def test_short_gif_delays_are_preserved(self):
        result = service.convert_gif({'gif': gif_blob(2, duration=20)})
        blob = base64.b64decode(result['pca'])
        self.assertEqual(struct.unpack_from('<HH', blob, 12), (20, 20))

    def test_transparent_gif_updates_are_coalesced_before_encoding(self):
        first = Image.new('P', (4, 4), 1)
        second = Image.new('P', (4, 4), 0)
        second.putpixel((0, 0), 2)
        palette = [0, 0, 0, 255, 0, 0, 0, 255, 0] + [0, 0, 0] * 253
        first.putpalette(palette)
        second.putpalette(palette)
        gif = io.BytesIO()
        first.save(gif, format='GIF', save_all=True, append_images=[second],
                   duration=[100, 100], loop=0, transparency=0,
                   disposal=[1, 1], optimize=False)

        result = service.convert_gif({
            'gif': base64.b64encode(gif.getvalue()).decode('ascii'),
            'fit': 'stretch',
        })
        blob = base64.b64decode(result['pca'])
        frame_bytes = 128 * 64 * 2
        frame_data = 12 + 2 * 2
        overlay_frame = frame_data + frame_bytes
        self.assertEqual(struct.unpack_from('<H', blob, overlay_frame)[0], 0x07E0)
        self.assertEqual(struct.unpack_from('<H', blob, overlay_frame + 32 * 2)[0], 0xF800)

    def test_fit_and_anchor_variants(self):
        for fit in ('crop', 'pad', 'stretch'):
            for anchor in ('start', 'center', 'end'):
                result = service.convert_gif({'gif': gif_blob(2), 'fit': fit, 'anchor': anchor})
                self.assertEqual(result['frames'], 2)
                with Image.open(io.BytesIO(base64.b64decode(result['preview']))) as preview:
                    self.assertEqual(preview.size, (512, 256))

    def test_invalid_options_and_data(self):
        for options in ({'gif':'!'}, {'gif':gif_blob(), 'frameSkip':0}, {'gif':gif_blob(), 'maxBytes':100}, {'gif':gif_blob(), 'fit':'bad'}):
            with self.assertRaises(ValueError): service.convert_gif(options)
        for name in ('../x', 'x y', 'x\r\n', 'ą', ''):
            with self.assertRaises(ValueError): service.animation_name(name)

    def test_upload_rechecks_space_before_sending(self):
        result = service.convert_gif({'gif': gif_blob(3)})
        with patch.object(service, 'device_json', return_value={'usable':True, 'maxUploadBytes':100}) as device:
            with self.assertRaisesRegex(ValueError, 'Storage changed'):
                service.upload_animation({'esp32_ip':'pixelclock.local'}, {'name':'clip','pca':result['pca']})
            self.assertEqual(device.call_count, 1)

    def test_truncated_payload_rejected(self):
        result = service.convert_gif({'gif':gif_blob(2)})
        with self.assertRaises(ValueError): service.validate_pca(base64.b64decode(result['pca'])[:-1])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        state = SimpleNamespace(get_config=lambda: {'esp32_ip':'pixelclock.local'})
        cls.httpd, cls.port = server.make_server(SimpleNamespace(), state, port=0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join()

    def test_convert_route_and_static_asset(self):
        base = 'http://127.0.0.1:' + str(self.port)
        with urlopen(base+'/animations.js') as response:
            self.assertIn(b'gifConvert', response.read())
        request = Request(base+'/api/animations/convert', data=json.dumps({'gif':gif_blob(3)}).encode(), headers={'Content-Type':'application/json'})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)['frames'], 3)

    def test_cross_origin_rejected(self):
        request = Request('http://127.0.0.1:'+str(self.port)+'/api/animations/convert', data=b'{}', headers={'Content-Type':'application/json','Origin':'https://example.com'})
        with self.assertRaises(HTTPError) as error: urlopen(request)
        self.assertEqual(error.exception.code, 403)


if __name__ == '__main__': unittest.main()
