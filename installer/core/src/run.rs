//! T4: the install run in order, emitting [`Event`]s; used by the window,
//! `--headless` and the tests alike.

use crate::api::{Command, Event, Selection, Source};
use std::sync::mpsc::Receiver;

pub fn run(_sel: Selection, _source: Source, _on: &mut dyn FnMut(Event), _commands: Receiver<Command>) {
    unimplemented!("T4")
}
