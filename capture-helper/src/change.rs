//! Change detection: is this frame the same as the last one sent for the same window?

use crate::desktop::WindowId;
use crate::pixels::{dhash, max_abs_diff, mean_abs_diff};

/// Largest Hamming distance between difference hashes that still counts as "the same".
pub const HASH_MAX_HAMMING: u32 = 2;
/// Mean absolute difference of the 32 x 32 thumbnails (gray levels) below which a frame can
/// count as unchanged.
pub const MAD_UNCHANGED_BELOW: f32 = 1.5;
/// Largest single-cell difference of the thumbnails (gray levels) below which a frame can
/// count as unchanged. The mean alone cannot see a small local change - a checkbox ticking
/// in a large window moves it by a few hundredths - so one clearly different cell is enough
/// to call the frame changed.
pub const CELL_UNCHANGED_BELOW: f32 = 8.0;
/// Windows remembered at once; the least recently used is dropped first.
pub const MAX_TRACKED_WINDOWS: usize = 8;

/// What is remembered of a frame. No pixels beyond the 32 x 32 grayscale thumbnail.
#[derive(Clone, Debug, PartialEq)]
pub struct Signature {
    pub hash: u64,
    pub thumb: Vec<f32>,
}

impl Signature {
    pub fn of_thumbnail(thumb: Vec<f32>) -> Self {
        Self {
            hash: dhash(&thumb),
            thumb,
        }
    }

    pub fn hash_hex(&self) -> String {
        format!("{:016x}", self.hash)
    }
}

pub fn hamming(a: u64, b: u64) -> u32 {
    (a ^ b).count_ones()
}

/// All three measures must agree before a frame is dropped as unchanged.
pub fn is_unchanged(previous: &Signature, current: &Signature) -> bool {
    hamming(previous.hash, current.hash) <= HASH_MAX_HAMMING
        && mean_abs_diff(&previous.thumb, &current.thumb) < MAD_UNCHANGED_BELOW
        && max_abs_diff(&previous.thumb, &current.thumb) < CELL_UNCHANGED_BELOW
}

/// Per-window signatures of the last frame that was sent, least recently used first.
#[derive(Default)]
pub struct ChangeMemory {
    entries: Vec<(WindowId, Signature)>,
}

impl ChangeMemory {
    /// Look a window up and mark it as most recently used.
    pub fn get(&mut self, id: &WindowId) -> Option<&Signature> {
        let index = self.entries.iter().position(|(key, _)| key == id)?;
        let entry = self.entries.remove(index);
        self.entries.push(entry);
        self.entries.last().map(|(_, signature)| signature)
    }

    pub fn put(&mut self, id: WindowId, signature: Signature) {
        self.entries.retain(|(key, _)| key != &id);
        if self.entries.len() >= MAX_TRACKED_WINDOWS {
            self.entries.remove(0);
        }
        self.entries.push((id, signature));
    }

    pub fn remove(&mut self, id: &WindowId) {
        self.entries.retain(|(key, _)| key != id);
    }

    pub fn clear(&mut self) {
        self.entries.clear();
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pixels::THUMB_SIDE;

    const CELLS: usize = THUMB_SIDE * THUMB_SIDE;

    fn sig(hash: u64, thumb: Vec<f32>) -> Signature {
        Signature { hash, thumb }
    }

    fn id(n: u32) -> WindowId {
        WindowId::parse(&format!("{n:08x}-0000-4000-8000-000000000000")).unwrap()
    }

    #[test]
    fn constants_are_the_documented_ones() {
        assert_eq!(HASH_MAX_HAMMING, 2);
        assert_eq!(MAD_UNCHANGED_BELOW, 1.5);
        assert_eq!(MAX_TRACKED_WINDOWS, 8);
    }

    #[test]
    fn hamming_counts_differing_bits() {
        assert_eq!(hamming(0, 0), 0);
        assert_eq!(hamming(0b1011, 0b0010), 2);
        assert_eq!(hamming(0, u64::MAX), 64);
    }

    #[test]
    fn identical_frames_are_unchanged() {
        let a = sig(0xABCD, vec![100.0; CELLS]);
        assert!(is_unchanged(&a, &a.clone()));
    }

    #[test]
    fn hamming_threshold_is_inclusive_at_two() {
        let base = sig(0, vec![100.0; CELLS]);
        assert!(is_unchanged(&base, &sig(0b1, vec![100.0; CELLS])));
        assert!(is_unchanged(&base, &sig(0b11, vec![100.0; CELLS])));
        assert!(!is_unchanged(&base, &sig(0b111, vec![100.0; CELLS])));
    }

    #[test]
    fn mean_difference_threshold_is_exclusive_at_one_and_a_half() {
        let base = sig(0, vec![100.0; CELLS]);
        assert!(is_unchanged(&base, &sig(0, vec![101.25; CELLS])));
        assert!(
            !is_unchanged(&base, &sig(0, vec![101.5; CELLS])),
            "exactly 1.5 is a change"
        );
        assert!(
            !is_unchanged(&base, &sig(0, vec![104.0; CELLS])),
            "equal hashes do not override"
        );
    }

    #[test]
    fn one_clearly_different_cell_is_a_change_even_with_a_tiny_mean() {
        let base = sig(0, vec![100.0; CELLS]);
        let mut local = vec![100.0; CELLS];
        local[500] = 107.9;
        assert!(is_unchanged(&base, &sig(0, local.clone())));
        local[500] = 108.0;
        assert!(mean_abs_diff(&base.thumb, &local) < 0.01);
        assert!(!is_unchanged(&base, &sig(0, local)));
    }

    #[test]
    fn memory_keeps_at_most_eight_windows_and_evicts_the_least_recently_used() {
        let mut memory = ChangeMemory::default();
        for n in 0..8 {
            memory.put(id(n), sig(u64::from(n), vec![]));
        }
        assert_eq!(memory.len(), 8);
        // Touch window 0 so that window 1 becomes the oldest.
        assert_eq!(memory.get(&id(0)).unwrap().hash, 0);
        memory.put(id(100), sig(100, vec![]));
        assert_eq!(memory.len(), MAX_TRACKED_WINDOWS);
        assert!(
            memory.get(&id(1)).is_none(),
            "least recently used was evicted"
        );
        assert!(memory.get(&id(0)).is_some(), "recently read survives");
        assert!(memory.get(&id(100)).is_some());
    }

    #[test]
    fn put_replaces_and_does_not_grow() {
        let mut memory = ChangeMemory::default();
        memory.put(id(1), sig(1, vec![]));
        memory.put(id(1), sig(2, vec![]));
        assert_eq!(memory.len(), 1);
        assert_eq!(memory.get(&id(1)).unwrap().hash, 2);
    }

    #[test]
    fn remove_and_clear() {
        let mut memory = ChangeMemory::default();
        memory.put(id(1), sig(1, vec![]));
        memory.put(id(2), sig(2, vec![]));
        memory.remove(&id(1));
        assert!(memory.get(&id(1)).is_none() && memory.get(&id(2)).is_some());
        memory.clear();
        assert!(memory.is_empty());
    }

    #[test]
    fn hash_hex_is_sixteen_lowercase_digits() {
        assert_eq!(sig(0xAB, vec![]).hash_hex(), "00000000000000ab");
    }
}
