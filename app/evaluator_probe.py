"""Live check of the answer evaluator with the configured chat model: answers
built on the demo documents, some with a planted error it must fail (a guess
in parentheses or inside a sentence, outside advice or websites, a wrong
number) and some clean or with supported asides and hedges it must pass.
Run it after changing answer_eval's prompt or scoring, or the chat model.

    docker compose run --rm --no-deps webapp python -m evaluator_probe [--repeat N]

Each case runs --repeat times (default 3), as one model call can be lucky:
30 evaluations by default (about one cent with Claude Haiku). Exits 1 if
any verdict differs from what is expected."""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import answer_eval
import llm
import seed_demo

DEMO = Path(__file__).resolve().parent.parent / "data" / "demo"
if not DEMO.is_dir():
    DEMO = seed_demo.DEMO_DIR   # in the container: /data/demo

BIKES = ("Yes. On Lakeshore Rail, full-size bicycles need a free bike reservation and each train takes at most "
         "four bikes [1]. Bikes are not carried on weekday trains arriving at Port Avalon between 07:00 and 09:30 [1].")
BIKE_Q = "Can I take a full-size bike on the train?"

# (name, question, answer, should pass)
CASES = [
    ("clean answer", BIKE_Q, BIKES, True),
    ("supported aside", "How much is an Off-Peak single from Port Avalon to Highfold?",
     "An Off-Peak single from Port Avalon to Highfold costs 24 crowns (the Anytime single is 38 crowns) [2].", True),
    ("supported hedge", "Can I take my full-size bike to Port Avalon on a weekday morning?",
     "Probably not before 09:30: bikes are not carried on weekday trains arriving at Port Avalon between 07:00 "
     "and 09:30 [1].", True),
    ("supported 'may'", "Can I bring my dog on the train?",
     "Yes, dogs travel free, up to two per passenger, on a lead [1]. Assistance dogs may travel too and do not "
     "count towards the limit [1].", True),
    ("guess in parentheses", BIKE_Q, BIKES.replace("On Lakeshore Rail,", "On Lakeshore Rail (likely North America),"),
     False),
    ("guess in a sentence", BIKE_Q,
     BIKES.replace("On Lakeshore Rail,", "On Lakeshore Rail, probably the busiest line in the region,"), False),
    ("standalone guess", BIKE_Q, BIKES + " Kestrel Bay appears to be in Canada.", False),
    ("outside website", BIKE_Q, BIKES + " You can also book bike spaces on Trainline.", False),
    ("outside advice", BIKE_Q, BIKES + " Bring a bike lock, as stations are often busy.", False),
    ("wrong number", BIKE_Q, BIKES.replace("at most four bikes", "at most six bikes"), False),
]


def _evidence() -> list[dict]:
    docs = dict(seed_demo.documents(DEMO))
    return [{"n": 1, "source_url": "adhoc://lakeshore-rail-luggage-bicycles-and-pets",
             "text": docs["Lakeshore Rail: luggage, bicycles and pets"]},
            {"n": 2, "source_url": "adhoc://lakeshore-rail-tickets-and-fares",
             "text": docs["Lakeshore Rail: tickets and fares"]}]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=3, help="runs per case (default 3)")
    args = parser.parse_args(argv)
    error = llm.config_error()
    if error:
        print(f"The chat model is not configured: {error}", file=sys.stderr)
        return 2
    chunks = _evidence()

    def check(case):
        name, question, answer, should_pass = case
        result = answer_eval.evaluate(question, answer, chunks, [])
        ok = result.passed == should_pass
        line = (f"{'ok  ' if ok else 'MISS'} {name:22} want {'pass' if should_pass else 'fail'} "
                f"got {'pass' if result.passed else 'fail'}  faithfulness {result.scores['faithfulness']:.2f}")
        if not ok:
            line += f"\n       unsupported: {result.unsupported_claims}  failed on: {result.failed_on}"
        return ok, line

    jobs = [case for case in CASES for _ in range(max(1, args.repeat))]
    print(f"Evaluator probe: {len(CASES)} cases x {max(1, args.repeat)} on {llm.label()}")
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(check, jobs))
    for _, line in results:
        print(line)
    misses = sum(not ok for ok, _ in results)
    print(f"{misses} of {len(results)} verdicts differ from what is expected")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
