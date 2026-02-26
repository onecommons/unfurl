

use ascent::ascent;

 pub fn Λ(&self, f: usize) -> usize {
     f + 1
 }
 
pub type Symbol<'a> = &'a str;

// type EntityName<'a> = Symbol<'a>;

type FooName<'a> = Symbol<'a>;

// TODO: it's fine for Topology to be limited to the stack, so we can &'a str everywhere and borrow from the python Strings. 

fn example(node: String) {
    let mut f = Topology::default();
    f.entity.push((&node, &node));

  println!(
            "queries (intermediate): {:#?}",
            f.entity.iter().collect::<Vec<_>>()
        );
}

ascent! {
    pub(crate) struct Topology<'a>;

    relation entity(Symbol<'a>, FooName<'a>);
}
#[test]
fn t() {
  example("test".to_string());
}
