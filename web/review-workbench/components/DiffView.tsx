import type { DiffOp } from "../lib/types";

/** Redline rendering of a server-computed diff: deletions struck, insertions underlined. */
export function DiffView({ ops }: { ops: DiffOp[] }) {
  if (ops.length === 0) {
    return <p className="diff">(empty)</p>;
  }
  return (
    <p className="diff">
      {ops.map((op, index) => {
        if (op.op === "insert") {
          return (
            <ins key={index}>
              <span className="visually-hidden">[added: </span>
              {op.text}
              <span className="visually-hidden">]</span>
            </ins>
          );
        }
        if (op.op === "delete") {
          return (
            <del key={index}>
              <span className="visually-hidden">[removed: </span>
              {op.text}
              <span className="visually-hidden">]</span>
            </del>
          );
        }
        return <span key={index}>{op.text}</span>;
      })}
    </p>
  );
}
