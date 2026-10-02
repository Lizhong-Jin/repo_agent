//! Recyclable parent/name nodes. Only live tasks, caches and their ancestors survive.
use std::ffi::{CStr, CString, OsStr};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

pub(crate) type NodeId = usize;
struct Node {
    parent: Option<NodeId>,
    name: CString,
    references: usize,
}
#[derive(Default)]
pub(crate) struct Paths {
    nodes: Vec<Option<Node>>,
    free: Vec<NodeId>,
    pub created: usize,
    pub copied_name_bytes: usize,
    live: usize,
    pub peak_live: usize,
}
impl Paths {
    pub fn root(&mut self) -> NodeId {
        self.insert(None, b"")
    }
    pub fn child(&mut self, parent: NodeId, name: &[u8]) -> NodeId {
        self.retain(parent);
        self.insert(Some(parent), name)
    }
    fn insert(&mut self, parent: Option<NodeId>, name: &[u8]) -> NodeId {
        let node = Some(Node {
            parent,
            name: CString::new(name).expect("directory component"),
            references: 1,
        });
        self.created += 1;
        self.copied_name_bytes += name.len() + 1;
        self.live += 1;
        self.peak_live = self.peak_live.max(self.live);
        if let Some(id) = self.free.pop() {
            self.nodes[id] = node;
            id
        } else {
            self.nodes.push(node);
            self.nodes.len() - 1
        }
    }
    pub fn retain(&mut self, id: NodeId) {
        self.nodes[id].as_mut().unwrap().references += 1;
    }
    pub fn release(&mut self, mut id: NodeId) {
        loop {
            let node = self.nodes[id].as_mut().unwrap();
            node.references -= 1;
            if node.references != 0 {
                break;
            }
            let parent = node.parent;
            self.nodes[id] = None;
            self.free.push(id);
            self.live -= 1;
            match parent {
                Some(next) => id = next,
                None => break,
            }
        }
    }
    pub fn parent(&self, id: NodeId) -> Option<NodeId> {
        self.nodes[id].as_ref().unwrap().parent
    }
    pub fn name(&self, id: NodeId) -> &CStr {
        &self.nodes[id].as_ref().unwrap().name
    }
    pub fn chain(&self, mut id: NodeId, scratch: &mut Vec<NodeId>) {
        scratch.clear();
        while let Some(parent) = self.parent(id) {
            scratch.push(id);
            id = parent;
        }
    }
    pub fn write_path(
        &self,
        id: NodeId,
        root: &Path,
        output: &mut PathBuf,
        scratch: &mut Vec<NodeId>,
    ) {
        self.chain(id, scratch);
        output.clear();
        output.push(root);
        for id in scratch.iter().rev() {
            output.push(OsStr::from_bytes(self.name(*id).to_bytes()));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_ancestors_release_iteratively_and_reuse_slots() {
        let mut paths = Paths::default();
        let root = paths.root();
        for _ in 0..1000 {
            let mut id = root;
            paths.retain(root);
            for _ in 0..100 {
                let child = paths.child(id, b"node");
                paths.release(id);
                id = child;
            }
            paths.release(id);
            assert_eq!(paths.live, 1);
        }
        assert_eq!(paths.nodes.len(), 101);
        paths.release(root);
        assert_eq!(paths.live, 0);
    }
}
