//! Unix directory enumeration pinned by fd. Never follow the final component.
use crate::directory_batch::Batch;
use crate::{Error, Result};
use std::ffi::{CStr, CString, OsString};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::Path;

pub struct Directory(*mut libc::DIR);
impl Drop for Directory {
    fn drop(&mut self) {
        // SAFETY: fdopendir transferred sole ownership; close exactly once.
        unsafe { libc::closedir(self.0) };
    }
}

impl Directory {
    pub fn open(path: &Path) -> Result<Self> {
        let name = CString::new(path.as_os_str().as_bytes())
            .map_err(|_| Error::value("embedded null byte", None))?;
        // SAFETY: name is a live NUL-terminated path; flags require a directory.
        let fd = unsafe {
            libc::open(
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            let error = std::io::Error::last_os_error();
            if matches!(error.raw_os_error(), Some(libc::ELOOP | libc::ENOTDIR)) {
                return Err(Error::value(
                    "Linux native 扫描期间目录变为符号链接或非目录：",
                    Some(path),
                ));
            }
            return Err(Error::io(error, Some(path)));
        }
        // SAFETY: fd is owned and open. fdopendir takes ownership only on success.
        let dir = unsafe { libc::fdopendir(fd) };
        if dir.is_null() {
            let error = std::io::Error::last_os_error();
            unsafe { libc::close(fd) };
            return Err(Error::io(error, Some(path)));
        }
        Ok(Self(dir))
    }

    pub fn metadata(&self, name: &CStr) -> Result<libc::stat> {
        crate::filesystem::metadata::stat_cstr(unsafe { libc::dirfd(self.0) }, name, None)
    }

    pub fn kind(&self, name: &CStr, dtype: u8, path: &Path) -> Result<(bool, bool)> {
        if dtype != libc::DT_UNKNOWN {
            return Ok((dtype == libc::DT_DIR, dtype == libc::DT_LNK));
        }
        let mut info = std::mem::MaybeUninit::<libc::stat>::uninit();
        // SAFETY: DIR owns a live fd, name is terminated, and successful fstatat
        // initializes the metadata. AT_SYMLINK_NOFOLLOW preserves entry identity.
        let rc = unsafe {
            libc::fstatat(
                libc::dirfd(self.0),
                name.as_ptr(),
                info.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if rc != 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ENOENT) {
                return Ok((false, false));
            }
            return Err(Error::io(
                error,
                Some(&path.join(OsString::from_vec(name.to_bytes().to_vec()))),
            ));
        }
        let mode = unsafe { info.assume_init() }.st_mode;
        Ok((
            mode & libc::S_IFMT == libc::S_IFDIR,
            mode & libc::S_IFMT == libc::S_IFLNK,
        ))
    }

    pub fn read_batch(
        &mut self,
        path: &Path,
        batch: &mut Batch,
        checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<bool> {
        unsafe { batch.read(self.0, Some(path), checkpoint) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn unknown_types_are_checked_relative_to_the_pinned_directory_without_following_links() {
        struct Tree(PathBuf);
        impl Drop for Tree {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = std::env::temp_dir().join(format!(
            "policy-dtype-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&root).unwrap();
        let tree = Tree(root.canonicalize().unwrap());
        fs::create_dir(tree.0.join("directory")).unwrap();
        fs::write(tree.0.join("file"), b"content").unwrap();
        symlink(tree.0.join("directory"), tree.0.join("link")).unwrap();
        let dir = Directory::open(&tree.0).unwrap();
        for (name, expected) in [
            ("directory", (true, false)),
            ("file", (false, false)),
            ("link", (false, true)),
            ("missing", (false, false)),
        ] {
            assert_eq!(
                dir.kind(&CString::new(name).unwrap(), libc::DT_UNKNOWN, &tree.0)
                    .unwrap(),
                expected
            );
        }
    }
}
