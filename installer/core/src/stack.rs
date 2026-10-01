//! T2: typed view of `manifests/stack.json` (the embedded [`crate::STACK_JSON`]).

#[derive(Debug, Clone)]
pub struct Stack {
    pub raw: serde_json::Value,
}

impl Stack {
    pub fn parse(_json: &str) -> Result<Stack, String> {
        unimplemented!("T2")
    }
    pub fn embedded() -> Stack {
        Stack::parse(crate::STACK_JSON).expect("embedded stack.json parses")
    }
}
