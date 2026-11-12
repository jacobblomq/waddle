//! All pixel work: decoding KWin's raw buffers, compositing popups, the change-detection
//! signature, and encoding the two images that leave the helper.

use std::io::Cursor;

use image::codecs::jpeg::JpegEncoder;
use image::codecs::png::PngEncoder;
use image::imageops::{self, FilterType};
use image::{ExtendedColorType, ImageEncoder, RgbImage};

use crate::protocol::Rect;

/// Upper bound on one raw window image (`width * height * 4` and `stride * height`).
pub const MAX_RAW_BYTES: u64 = 256 * 1024 * 1024;
/// Side of the grayscale thumbnail kept per window for change detection.
pub const THUMB_SIDE: usize = 32;
/// The difference hash compares horizontal neighbours of a 9 x 8 grid: 8 x 8 = 64 bits.
pub const HASH_GRID_W: usize = 9;
pub const HASH_GRID_H: usize = 8;
pub const JPEG_QUALITY: u8 = 80;
/// A hash bit is set only when the left cell is brighter by more than this many gray levels,
/// so flat areas hash to stable zeros instead of rounding noise.
pub const HASH_MARGIN: f32 = 0.5;

// QImage::Format values KWin may put in the reply metadata.
pub const FORMAT_RGB32: u32 = 4;
pub const FORMAT_ARGB32: u32 = 5;
pub const FORMAT_ARGB32_PREMULTIPLIED: u32 = 6;
pub const FORMAT_RGBX8888: u32 = 16;
pub const FORMAT_RGBA8888: u32 = 17;
pub const FORMAT_RGBA8888_PREMULTIPLIED: u32 = 18;

/// Reply metadata describing the raw buffer on the pipe.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RawMeta {
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub format: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PixelError {
    /// Zero-sized, or a stride smaller than one row of pixels.
    BadGeometry,
    TooLarge,
    UnsupportedFormat(u32),
    /// The pipe did not deliver exactly `stride * height` bytes.
    LengthMismatch,
    Encode,
}

impl PixelError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::BadGeometry | Self::LengthMismatch => "bad_reply",
            Self::TooLarge => "too_large",
            Self::UnsupportedFormat(_) => "bad_format",
            Self::Encode => "encode_failed",
        }
    }

    pub fn message(&self) -> String {
        match self {
            Self::BadGeometry => "KWin reported an impossible image geometry".into(),
            Self::TooLarge => "window image exceeds the size limit".into(),
            Self::UnsupportedFormat(f) => format!("unsupported QImage format {f}"),
            Self::LengthMismatch => "pixel data does not match stride * height".into(),
            Self::Encode => "could not encode the image".into(),
        }
    }
}

/// A rectangle in image pixels, always inside the image it was computed for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PxRect {
    pub x: u32,
    pub y: u32,
    pub w: u32,
    pub h: u32,
}

fn is_supported(format: u32) -> bool {
    matches!(
        format,
        FORMAT_RGB32
            | FORMAT_ARGB32
            | FORMAT_ARGB32_PREMULTIPLIED
            | FORMAT_RGBX8888
            | FORMAT_RGBA8888
            | FORMAT_RGBA8888_PREMULTIPLIED
    )
}

/// Validate the metadata and return how many bytes the pipe must deliver.
pub fn expected_len(meta: &RawMeta) -> Result<usize, PixelError> {
    if meta.width == 0 || meta.height == 0 {
        return Err(PixelError::BadGeometry);
    }
    let row = u64::from(meta.width) * 4;
    let total = u64::from(meta.stride) * u64::from(meta.height);
    if row * u64::from(meta.height) > MAX_RAW_BYTES || total > MAX_RAW_BYTES {
        return Err(PixelError::TooLarge);
    }
    if u64::from(meta.stride) < row {
        return Err(PixelError::BadGeometry);
    }
    if !is_supported(meta.format) {
        return Err(PixelError::UnsupportedFormat(meta.format));
    }
    Ok(total as usize)
}

/// Convert a raw buffer to opaque RGB8, dropping stride padding and alpha.
///
/// Premultiplied formats are taken as they are: the colour channels already hold what the
/// window looks like over black, which is what an opaque image needs.
pub fn to_rgb(meta: &RawMeta, data: &[u8]) -> Result<RgbImage, PixelError> {
    if data.len() != expected_len(meta)? {
        return Err(PixelError::LengthMismatch);
    }
    let (width, height, stride) = (
        meta.width as usize,
        meta.height as usize,
        meta.stride as usize,
    );
    // The 32-bit QImage formats are one host-endian 0xAARRGGBB word per pixel; the 8888
    // formats are bytes in R, G, B, A order on every host.
    let word_format = matches!(
        meta.format,
        FORMAT_RGB32 | FORMAT_ARGB32 | FORMAT_ARGB32_PREMULTIPLIED
    );
    let mut out = vec![0u8; width * height * 3];
    for (row, dst) in data
        .chunks_exact(stride)
        .zip(out.chunks_exact_mut(width * 3))
    {
        for (src, rgb) in row[..width * 4]
            .chunks_exact(4)
            .zip(dst.chunks_exact_mut(3))
        {
            if word_format {
                let word = u32::from_ne_bytes([src[0], src[1], src[2], src[3]]);
                rgb[0] = (word >> 16) as u8;
                rgb[1] = (word >> 8) as u8;
                rgb[2] = word as u8;
            } else {
                rgb.copy_from_slice(&src[..3]);
            }
        }
    }
    RgbImage::from_raw(meta.width, meta.height, out).ok_or(PixelError::BadGeometry)
}

/// Draw `top` onto `base` with its top-left corner at (`dx`, `dy`), clipped to `base`.
pub fn overlay(base: &mut RgbImage, top: &RgbImage, dx: i64, dy: i64) {
    let (bw, bh) = (i64::from(base.width()), i64::from(base.height()));
    let (tw, th) = (i64::from(top.width()), i64::from(top.height()));
    let (x0, y0) = (dx.max(0), dy.max(0));
    let (x1, y1) = ((dx + tw).min(bw), (dy + th).min(bh));
    if x0 >= x1 || y0 >= y1 {
        return;
    }
    let span = ((x1 - x0) * 3) as usize;
    let base_stride = (bw * 3) as usize;
    let top_stride = (tw * 3) as usize;
    let top_raw: &[u8] = top.as_raw();
    let base_raw: &mut [u8] = base;
    for y in y0..y1 {
        let src = (y - dy) as usize * top_stride + ((x0 - dx) * 3) as usize;
        let dst = y as usize * base_stride + (x0 * 3) as usize;
        base_raw[dst..dst + span].copy_from_slice(&top_raw[src..src + span]);
    }
}

/// Map a window-logical rectangle to image pixels: scaled, rounded outward, clipped.
/// `None` when nothing of it lies inside the image.
pub fn logical_to_px(rect: &Rect, scale: f64, img_w: u32, img_h: u32) -> Option<PxRect> {
    let x0 = (rect.x * scale).floor().clamp(0.0, f64::from(img_w)) as u32;
    let y0 = (rect.y * scale).floor().clamp(0.0, f64::from(img_h)) as u32;
    let x1 = ((rect.x + rect.w) * scale)
        .ceil()
        .clamp(0.0, f64::from(img_w)) as u32;
    let y1 = ((rect.y + rect.h) * scale)
        .ceil()
        .clamp(0.0, f64::from(img_h)) as u32;
    (x1 > x0 && y1 > y0).then_some(PxRect {
        x: x0,
        y: y0,
        w: x1 - x0,
        h: y1 - y0,
    })
}

/// Box-average the image to a `THUMB_SIDE` x `THUMB_SIDE` grayscale grid (values 0-255).
///
/// Masked pixels are left out of the average, so whatever happens inside a live region
/// cannot move the result; a fully masked cell is 0.
pub fn thumbnail(img: &RgbImage, masks: &[PxRect]) -> Vec<f32> {
    let (w, h) = (img.width() as usize, img.height() as usize);
    let cell_of_col: Vec<usize> = (0..w).map(|x| x * THUMB_SIDE / w).collect();
    let mut sums = vec![0u64; THUMB_SIDE * THUMB_SIDE];
    let mut counts = vec![0u32; THUMB_SIDE * THUMB_SIDE];
    let mut masked_row = vec![false; w];
    for (y, row) in img.as_raw().chunks_exact(w * 3).enumerate() {
        let mut any_mask = false;
        for m in masks {
            if (m.y as usize..(m.y + m.h) as usize).contains(&y) {
                if !any_mask {
                    masked_row.fill(false);
                    any_mask = true;
                }
                let end = ((m.x + m.w) as usize).min(w);
                masked_row[(m.x as usize).min(end)..end].fill(true);
            }
        }
        let base = (y * THUMB_SIDE / h) * THUMB_SIDE;
        for (x, px) in row.chunks_exact(3).enumerate() {
            if any_mask && masked_row[x] {
                continue;
            }
            let luma =
                (u32::from(px[0]) * 299 + u32::from(px[1]) * 587 + u32::from(px[2]) * 114) / 1000;
            let cell = base + cell_of_col[x];
            sums[cell] += u64::from(luma);
            counts[cell] += 1;
        }
    }
    sums.iter()
        .zip(&counts)
        .map(|(&sum, &count)| {
            if count == 0 {
                0.0
            } else {
                sum as f32 / count as f32
            }
        })
        .collect()
}

/// Overlap of output cell `i` (of `out`) with input cell `j` (of `inp`), in input cells.
fn overlap(i: usize, out: usize, j: usize, inp: usize) -> f32 {
    let lo = (i * inp) as f32 / out as f32;
    let hi = ((i + 1) * inp) as f32 / out as f32;
    (hi.min((j + 1) as f32) - lo.max(j as f32)).max(0.0)
}

/// Area-weighted downscale of the thumbnail to the 9 x 8 hash grid.
fn hash_grid(thumb: &[f32]) -> Vec<f32> {
    let mut grid = vec![0.0f32; HASH_GRID_W * HASH_GRID_H];
    for gy in 0..HASH_GRID_H {
        for gx in 0..HASH_GRID_W {
            let (mut sum, mut weight) = (0.0f32, 0.0f32);
            for ty in 0..THUMB_SIDE {
                let wy = overlap(gy, HASH_GRID_H, ty, THUMB_SIDE);
                if wy == 0.0 {
                    continue;
                }
                for tx in 0..THUMB_SIDE {
                    let wgt = wy * overlap(gx, HASH_GRID_W, tx, THUMB_SIDE);
                    sum += wgt * thumb[ty * THUMB_SIDE + tx];
                    weight += wgt;
                }
            }
            grid[gy * HASH_GRID_W + gx] = sum / weight;
        }
    }
    grid
}

/// 64-bit difference hash: one bit per horizontal neighbour pair of the 9 x 8 grid, set when
/// the left cell is brighter by more than [`HASH_MARGIN`].
pub fn dhash(thumb: &[f32]) -> u64 {
    let grid = hash_grid(thumb);
    let mut hash = 0u64;
    for gy in 0..HASH_GRID_H {
        for gx in 0..HASH_GRID_W - 1 {
            let left = grid[gy * HASH_GRID_W + gx];
            let right = grid[gy * HASH_GRID_W + gx + 1];
            hash = (hash << 1) | u64::from(left - right > HASH_MARGIN);
        }
    }
    hash
}

pub fn mean_abs_diff(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| (x - y).abs()).sum::<f32>() / a.len().max(1) as f32
}

pub fn max_abs_diff(a: &[f32], b: &[f32]) -> f32 {
    a.iter()
        .zip(b)
        .map(|(x, y)| (x - y).abs())
        .fold(0.0, f32::max)
}

/// A `size` x `size` square centred on (`cx`, `cy`) image pixels, shifted to lie inside the
/// image and shrunk to the image where the image is smaller than `size`.
pub fn crop_rect(img_w: u32, img_h: u32, cx: f64, cy: f64, size: u32) -> PxRect {
    let axis = |len: u32, centre: f64| -> (u32, u32) {
        let extent = size.min(len);
        let max_start = f64::from(len - extent);
        let start = (centre - f64::from(extent) / 2.0).round();
        let start = if start.is_nan() {
            0.0
        } else {
            start.clamp(0.0, max_start)
        };
        (start as u32, extent)
    };
    let (x, w) = axis(img_w, cx);
    let (y, h) = axis(img_h, cy);
    PxRect { x, y, w, h }
}

/// Size of the overview: the longest side becomes `max` when the image is larger; an image
/// that already fits is not enlarged.
pub fn overview_size(w: u32, h: u32, max: u32) -> (u32, u32) {
    let longest = w.max(h);
    if longest <= max {
        return (w, h);
    }
    let scale = |side: u32| {
        ((u64::from(side) * u64::from(max) + u64::from(longest) / 2) / u64::from(longest)).max(1)
            as u32
    };
    (scale(w), scale(h))
}

/// The downscaled JPEG overview and its size.
pub fn encode_overview(img: &RgbImage, max: u32) -> Result<(Vec<u8>, u32, u32), PixelError> {
    let (w, h) = overview_size(img.width(), img.height(), max);
    let mut out = Vec::new();
    let encoder = JpegEncoder::new_with_quality(Cursor::new(&mut out), JPEG_QUALITY);
    if (w, h) == img.dimensions() {
        encoder.write_image(img.as_raw(), w, h, ExtendedColorType::Rgb8)
    } else {
        let small = imageops::resize(img, w, h, FilterType::Triangle);
        encoder.write_image(small.as_raw(), w, h, ExtendedColorType::Rgb8)
    }
    .map_err(|_| PixelError::Encode)?;
    Ok((out, w, h))
}

/// The native-resolution PNG crop.
pub fn encode_crop(img: &RgbImage, rect: PxRect) -> Result<Vec<u8>, PixelError> {
    let part = imageops::crop_imm(img, rect.x, rect.y, rect.w, rect.h).to_image();
    let mut out = Vec::new();
    PngEncoder::new(Cursor::new(&mut out))
        .write_image(part.as_raw(), rect.w, rect.h, ExtendedColorType::Rgb8)
        .map_err(|_| PixelError::Encode)?;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::Rgb;

    fn meta(width: u32, height: u32, stride: u32, format: u32) -> RawMeta {
        RawMeta {
            width,
            height,
            stride,
            format,
        }
    }

    /// Encode one pixel the way QImage stores it for `format`.
    fn encode(format: u32, [r, g, b]: [u8; 3], alpha: u8) -> [u8; 4] {
        match format {
            FORMAT_RGB32 | FORMAT_ARGB32 | FORMAT_ARGB32_PREMULTIPLIED => {
                (u32::from(alpha) << 24 | u32::from(r) << 16 | u32::from(g) << 8 | u32::from(b))
                    .to_ne_bytes()
            }
            _ => [r, g, b, alpha],
        }
    }

    #[test]
    fn converts_every_supported_format_to_rgb() {
        let colours = [[250u8, 20, 3], [1, 200, 90], [7, 8, 255], [0, 0, 0]];
        for format in [
            FORMAT_RGB32,
            FORMAT_ARGB32,
            FORMAT_ARGB32_PREMULTIPLIED,
            FORMAT_RGBX8888,
            FORMAT_RGBA8888,
            FORMAT_RGBA8888_PREMULTIPLIED,
        ] {
            let data: Vec<u8> = colours
                .iter()
                .flat_map(|c| encode(format, *c, 0xff))
                .collect();
            let img = to_rgb(&meta(2, 2, 8, format), &data).unwrap();
            let got: Vec<[u8; 3]> = img.pixels().map(|p| p.0).collect();
            assert_eq!(got, colours, "format {format}");
        }
    }

    #[cfg(target_endian = "little")]
    #[test]
    fn argb32_is_bgra_in_memory_on_little_endian() {
        let img = to_rgb(&meta(1, 1, 4, FORMAT_ARGB32), &[0x10, 0x20, 0x30, 0xff]).unwrap();
        assert_eq!(img.get_pixel(0, 0).0, [0x30, 0x20, 0x10]);
        let img = to_rgb(&meta(1, 1, 4, FORMAT_RGBA8888), &[0x10, 0x20, 0x30, 0xff]).unwrap();
        assert_eq!(img.get_pixel(0, 0).0, [0x10, 0x20, 0x30]);
    }

    #[test]
    fn stride_padding_is_skipped() {
        // 3 pixels per row, 4 bytes of padding after each row, filled with a value that
        // would be visible if it leaked into the image.
        let mut data = Vec::new();
        for row in 0..2u8 {
            for col in 0..3u8 {
                data.extend_from_slice(&encode(FORMAT_RGBA8888, [row * 10 + col, 100, 200], 0xff));
            }
            data.extend_from_slice(&[0xEE; 4]);
        }
        let img = to_rgb(&meta(3, 2, 16, FORMAT_RGBA8888), &data).unwrap();
        assert_eq!(img.dimensions(), (3, 2));
        for (x, y, p) in img.enumerate_pixels() {
            assert_eq!(p.0, [(y * 10 + x) as u8, 100, 200]);
        }
    }

    #[test]
    fn rejects_bad_geometry_formats_and_lengths() {
        assert_eq!(
            expected_len(&meta(0, 4, 0, FORMAT_ARGB32)),
            Err(PixelError::BadGeometry)
        );
        assert_eq!(
            expected_len(&meta(4, 0, 16, FORMAT_ARGB32)),
            Err(PixelError::BadGeometry)
        );
        assert_eq!(
            expected_len(&meta(4, 4, 15, FORMAT_ARGB32)),
            Err(PixelError::BadGeometry)
        );
        // Format_RGB16 (7), Format_RGBA64 (26), Format_Invalid (0)
        for format in [0, 3, 7, 26, 99] {
            assert_eq!(
                expected_len(&meta(4, 4, 16, format)),
                Err(PixelError::UnsupportedFormat(format))
            );
        }
        assert_eq!(expected_len(&meta(4, 4, 20, FORMAT_ARGB32)), Ok(80));
        assert_eq!(
            to_rgb(&meta(4, 4, 16, FORMAT_ARGB32), &[0; 63]),
            Err(PixelError::LengthMismatch)
        );
        assert_eq!(
            to_rgb(&meta(4, 4, 16, FORMAT_ARGB32), &[0; 65]),
            Err(PixelError::LengthMismatch)
        );
    }

    #[test]
    fn size_limit_is_enforced_on_pixels_and_on_stride() {
        // 8192 x 8192 x 4 is exactly 256 MiB: allowed. One more row is not.
        assert_eq!(
            expected_len(&meta(8192, 8192, 8192 * 4, FORMAT_ARGB32)),
            Ok(MAX_RAW_BYTES as usize)
        );
        assert_eq!(
            expected_len(&meta(8192, 8193, 8192 * 4, FORMAT_ARGB32)),
            Err(PixelError::TooLarge)
        );
        assert_eq!(
            expected_len(&meta(65535, 65535, 65535 * 4, FORMAT_ARGB32)),
            Err(PixelError::TooLarge)
        );
        // Small image, absurd stride.
        assert_eq!(
            expected_len(&meta(1, 1024, u32::MAX, FORMAT_ARGB32)),
            Err(PixelError::TooLarge)
        );
    }

    fn solid(w: u32, h: u32, v: u8) -> RgbImage {
        RgbImage::from_pixel(w, h, Rgb([v, v, v]))
    }

    #[test]
    fn overlay_places_and_clips() {
        let mut base = solid(10, 10, 0);
        overlay(&mut base, &solid(4, 4, 255), 2, 3);
        let lit: Vec<(u32, u32)> = base
            .enumerate_pixels()
            .filter(|(_, _, p)| p.0[0] == 255)
            .map(|(x, y, _)| (x, y))
            .collect();
        assert_eq!(lit.len(), 16);
        assert!(lit
            .iter()
            .all(|&(x, y)| (2..6).contains(&x) && (3..7).contains(&y)));

        let mut base = solid(10, 10, 0);
        overlay(&mut base, &solid(4, 4, 255), -2, 8);
        let lit: Vec<(u32, u32)> = base
            .enumerate_pixels()
            .filter(|(_, _, p)| p.0[0] == 255)
            .map(|(x, y, _)| (x, y))
            .collect();
        assert_eq!(lit, vec![(0, 8), (1, 8), (0, 9), (1, 9)]);

        let mut base = solid(10, 10, 0);
        for (dx, dy) in [(10, 0), (0, 10), (-4, 0), (0, -4), (i64::from(i32::MAX), 0)] {
            overlay(&mut base, &solid(4, 4, 255), dx, dy);
        }
        assert!(
            base.pixels().all(|p| p.0 == [0, 0, 0]),
            "fully outside draws nothing"
        );
    }

    #[test]
    fn overlay_copies_the_right_source_pixels() {
        let mut top = solid(3, 2, 0);
        for (x, y, p) in top.enumerate_pixels_mut() {
            *p = Rgb([x as u8, y as u8, 9]);
        }
        let mut base = solid(4, 4, 100);
        overlay(&mut base, &top, -1, 3);
        assert_eq!(base.get_pixel(0, 3).0, [1, 0, 9]);
        assert_eq!(base.get_pixel(1, 3).0, [2, 0, 9]);
        assert_eq!(base.get_pixel(2, 3).0, [100, 100, 100]);
    }

    #[test]
    fn logical_rects_scale_round_outward_and_clip() {
        let r = Rect {
            x: 10.2,
            y: 5.0,
            w: 10.0,
            h: 4.4,
        };
        assert_eq!(
            logical_to_px(&r, 1.5, 1000, 1000),
            Some(PxRect {
                x: 15,
                y: 7,
                w: 16,
                h: 8
            })
        );
        let r = Rect {
            x: -5.0,
            y: 90.0,
            w: 20.0,
            h: 50.0,
        };
        assert_eq!(
            logical_to_px(&r, 1.0, 100, 100),
            Some(PxRect {
                x: 0,
                y: 90,
                w: 15,
                h: 10
            })
        );
        assert_eq!(
            logical_to_px(
                &Rect {
                    x: 200.0,
                    y: 0.0,
                    w: 5.0,
                    h: 5.0
                },
                1.0,
                100,
                100
            ),
            None
        );
        assert_eq!(
            logical_to_px(
                &Rect {
                    x: 5.0,
                    y: 5.0,
                    w: 0.0,
                    h: 5.0
                },
                1.0,
                100,
                100
            ),
            None
        );
    }

    #[test]
    fn thumbnail_is_a_box_average() {
        let mut img = solid(64, 64, 0);
        // Top-left 2x2 block (exactly thumbnail cell 0) half white.
        img.put_pixel(0, 0, Rgb([255, 255, 255]));
        img.put_pixel(1, 1, Rgb([255, 255, 255]));
        let thumb = thumbnail(&img, &[]);
        assert_eq!(thumb.len(), THUMB_SIDE * THUMB_SIDE);
        assert!((thumb[0] - 127.5).abs() < 0.01, "{}", thumb[0]);
        assert!(thumb[1..].iter().all(|&v| v == 0.0));
    }

    #[test]
    fn thumbnail_handles_images_smaller_than_the_grid() {
        let thumb = thumbnail(&solid(5, 3, 200), &[]);
        assert_eq!(thumb.len(), THUMB_SIDE * THUMB_SIDE);
        assert!(thumb.iter().all(|&v| v == 0.0 || v == 200.0));
        assert_eq!(thumb.iter().filter(|&&v| v == 200.0).count(), 15);
    }

    #[test]
    fn masked_pixels_do_not_reach_the_thumbnail_or_the_hash() {
        let quiet = solid(320, 320, 40);
        let mut busy = quiet.clone();
        for y in 100..160 {
            for x in 50..210 {
                busy.put_pixel(x, y, Rgb([255, (x % 256) as u8, 0]));
            }
        }
        let mask = [PxRect {
            x: 50,
            y: 100,
            w: 160,
            h: 60,
        }];
        assert!(
            max_abs_diff(&thumbnail(&quiet, &[]), &thumbnail(&busy, &[])) > 50.0,
            "visible unmasked"
        );
        let (a, b) = (thumbnail(&quiet, &mask), thumbnail(&busy, &mask));
        assert_eq!(a, b, "identical once the region is masked");
        assert_eq!(dhash(&a), dhash(&b));
        // A change just outside the mask is still seen.
        busy.put_pixel(49, 100, Rgb([255, 255, 255]));
        assert_ne!(thumbnail(&quiet, &mask), thumbnail(&busy, &mask));
    }

    #[test]
    fn overlapping_and_out_of_range_masks_are_safe() {
        let img = solid(40, 40, 90);
        let masks = [
            PxRect {
                x: 0,
                y: 0,
                w: 30,
                h: 30,
            },
            PxRect {
                x: 20,
                y: 20,
                w: 20,
                h: 20,
            },
            PxRect {
                x: 35,
                y: 35,
                w: 500,
                h: 500,
            },
        ];
        let thumb = thumbnail(&img, &masks);
        assert!(thumb.iter().all(|&v| v == 0.0 || v == 90.0));
    }

    fn gradient(w: u32, h: u32, rising: bool) -> RgbImage {
        let mut img = RgbImage::new(w, h);
        for (x, _, p) in img.enumerate_pixels_mut() {
            let v = (x * 255 / (w - 1)) as u8;
            let v = if rising { v } else { 255 - v };
            *p = Rgb([v, v, v]);
        }
        img
    }

    #[test]
    fn dhash_bits_follow_the_gradient_direction() {
        assert_eq!(
            dhash(&thumbnail(&gradient(288, 128, true), &[])),
            0,
            "left never brighter"
        );
        assert_eq!(
            dhash(&thumbnail(&gradient(288, 128, false), &[])),
            u64::MAX,
            "left always brighter"
        );
        assert_eq!(
            dhash(&thumbnail(&solid(100, 100, 77), &[])),
            0,
            "flat image: no bit set"
        );
    }

    #[test]
    fn dhash_is_stable_across_resolution() {
        let small = dhash(&thumbnail(&gradient(320, 200, false), &[]));
        let large = dhash(&thumbnail(&gradient(1280, 800, false), &[]));
        assert_eq!(small, large);
    }

    #[test]
    fn diffs() {
        let a = vec![0.0, 10.0, 20.0, 30.0];
        let b = vec![0.0, 12.0, 20.0, 20.0];
        assert_eq!(mean_abs_diff(&a, &b), 3.0);
        assert_eq!(max_abs_diff(&a, &b), 10.0);
        assert_eq!(mean_abs_diff(&a, &a), 0.0);
    }

    #[test]
    fn crop_is_centred_when_there_is_room() {
        assert_eq!(
            crop_rect(2000, 1000, 1000.0, 500.0, 512),
            PxRect {
                x: 744,
                y: 244,
                w: 512,
                h: 512
            }
        );
    }

    #[test]
    fn crop_is_clamped_at_every_edge() {
        let size = 512;
        assert_eq!(
            crop_rect(2000, 1000, 0.0, 0.0, size),
            PxRect {
                x: 0,
                y: 0,
                w: 512,
                h: 512
            }
        );
        assert_eq!(
            crop_rect(2000, 1000, 1999.0, 999.0, size),
            PxRect {
                x: 1488,
                y: 488,
                w: 512,
                h: 512
            }
        );
        assert_eq!(
            crop_rect(2000, 1000, -5000.0, 1e9, size),
            PxRect {
                x: 0,
                y: 488,
                w: 512,
                h: 512
            }
        );
        assert_eq!(
            crop_rect(2000, 1000, 100.0, 900.0, size),
            PxRect {
                x: 0,
                y: 488,
                w: 512,
                h: 512
            }
        );
        assert_eq!(crop_rect(2000, 1000, f64::NAN, 500.0, size).x, 0);
    }

    #[test]
    fn crop_shrinks_to_images_smaller_than_the_crop() {
        assert_eq!(
            crop_rect(300, 1000, 150.0, 500.0, 512),
            PxRect {
                x: 0,
                y: 244,
                w: 300,
                h: 512
            }
        );
        assert_eq!(
            crop_rect(300, 200, 1.0, 1.0, 512),
            PxRect {
                x: 0,
                y: 0,
                w: 300,
                h: 200
            }
        );
        let r = crop_rect(1, 1, 0.0, 0.0, 16);
        assert_eq!(
            r,
            PxRect {
                x: 0,
                y: 0,
                w: 1,
                h: 1
            }
        );
    }

    #[test]
    fn crop_always_lies_inside_the_image() {
        for (w, h) in [(1u32, 1u32), (17, 900), (640, 480), (3840, 2160)] {
            for size in [16u32, 512, 2048] {
                for (cx, cy) in [
                    (-1.0, -1.0),
                    (0.4, 0.6),
                    (f64::from(w), f64::from(h)),
                    (1e7, -1e7),
                ] {
                    let r = crop_rect(w, h, cx, cy, size);
                    assert!(
                        r.w >= 1 && r.h >= 1 && r.x + r.w <= w && r.y + r.h <= h,
                        "{r:?} in {w}x{h}"
                    );
                }
            }
        }
    }

    #[test]
    fn overview_sizes() {
        assert_eq!(overview_size(3840, 2160, 1024), (1024, 576));
        assert_eq!(overview_size(1080, 1920, 1024), (576, 1024));
        assert_eq!(overview_size(800, 600, 1024), (800, 600), "never enlarged");
        assert_eq!(overview_size(1024, 10, 1024), (1024, 10));
        assert_eq!(
            overview_size(4096, 1, 64),
            (64, 1),
            "thin images keep at least one pixel"
        );
    }

    #[test]
    fn overview_is_a_decodable_jpeg_of_the_announced_size() {
        let (bytes, w, h) = encode_overview(&gradient(400, 200, true), 100).unwrap();
        assert_eq!((w, h), (100, 50));
        assert_eq!(&bytes[..3], &[0xFF, 0xD8, 0xFF]);
        let decoded = image::load_from_memory_with_format(&bytes, image::ImageFormat::Jpeg)
            .unwrap()
            .to_rgb8();
        assert_eq!(decoded.dimensions(), (100, 50));
        assert!(decoded.get_pixel(2, 25).0[0] < 40 && decoded.get_pixel(97, 25).0[0] > 215);
    }

    #[test]
    fn crop_is_a_lossless_png_of_the_requested_pixels() {
        let mut img = solid(50, 40, 10);
        for (x, y, p) in img.enumerate_pixels_mut() {
            *p = Rgb([x as u8, y as u8, 200]);
        }
        let rect = PxRect {
            x: 30,
            y: 5,
            w: 20,
            h: 10,
        };
        let bytes = encode_crop(&img, rect).unwrap();
        assert_eq!(&bytes[..8], b"\x89PNG\r\n\x1a\n");
        let decoded = image::load_from_memory_with_format(&bytes, image::ImageFormat::Png)
            .unwrap()
            .to_rgb8();
        assert_eq!(decoded.dimensions(), (20, 10));
        for (x, y, p) in decoded.enumerate_pixels() {
            assert_eq!(p.0, [(x + 30) as u8, (y + 5) as u8, 200]);
        }
    }
}
