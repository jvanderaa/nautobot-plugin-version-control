import json

from django.core.exceptions import ObjectDoesNotExist
from django.db import connection
from django.db.models import Sum
from django.contrib.contenttypes.models import ContentType

from dolt.models import (
    Branch,
    Conflicts,
    ConstraintViolations,
    Commit,
)
from dolt.utils import author_from_user
from dolt.tables import (
    ConflictsSummaryTable,
    ConflictsTable,
    ConstraintViolationsTable,
)
from dolt.versioning import query_on_branch

# TODO: this file should be named "conflicts.py"


def get_conflicts_count_for_merge(src, dest):
    """
    Gather a merge-candidate for `src` and `dest`,
    then return Conflicts created by the merge

    TODO: currently we return conflicts summary,
        we need granular row-level conflicts and
        constraint violations.
    """
    mc = get_or_make_merge_candidate(src, dest)
    with query_on_branch(mc):
        c = Conflicts.objects.all().aggregate(Sum("num_conflicts"))
        v = ConstraintViolations.objects.all().aggregate(Sum("num_violations"))
        num_conflicts = c["num_conflicts__sum"] if c["num_conflicts__sum"] else 0
        num_violations = v["num_violations__sum"] if v["num_violations__sum"] else 0
        return num_conflicts + num_violations


def get_conflicts_for_merge(src, dest):
    """
    Gather a merge-candidate for `src` and `dest`,
    then return Conflicts created by the merge

    TODO: currently we return conflicts summary,
        we need granular row-level conflicts and
        constraint violations.
    """
    mc = get_or_make_merge_candidate(src, dest)
    with query_on_branch(mc):
        conflicts = MergeConflicts(src, dest)
        return {
            "summary": conflicts.make_conflict_summary_table(),
            "conflicts": conflicts.make_conflict_table(),
            "violations": conflicts.make_constraint_violations_table(),
        }


def merge_candidate_exists(src, dest):
    name = _merge_candidate_name(src, dest)
    try:
        mc = Branch.objects.get(name=name)
        return merge_candidate_is_fresh(mc, src, dest)
    except Branch.DoesNotExist:
        return False


def merge_candidate_is_fresh(mc, src, dest):
    """
    A merge candidate (MC) is considered "fresh" if the
    source and destination branches used to create the
    MC are unchanged since the MC was created.
    """
    if not mc:
        return False
    src_stable = Commit.merge_base(mc, src) == src.hash
    dest_stable = Commit.merge_base(mc, dest) == dest.hash
    return src_stable and dest_stable


def get_merge_candidate(src, dest):
    if merge_candidate_exists(src, dest):
        name = _merge_candidate_name(src, dest)
        return Branch.objects.get(name=name)
    return None


def make_merge_candidate(src, dest):
    name = _merge_candidate_name(src, dest)
    Branch(name=name, starting_branch=dest).save()
    with connection.cursor() as cursor:
        cursor.execute("SET @@dolt_force_transaction_commit = 1;")
        cursor.execute(f"""SELECT dolt_checkout("{name}") FROM dual;""")
        cursor.execute(f"""SELECT dolt_merge("{src}") FROM dual;""")
        cursor.execute(f"""SELECT dolt_add("-A") FROM dual;""")
        msg = f"""creating merge candidate with src: "{src}" and dest: "{dest}"."""
        cursor.execute(
            f"""SELECT dolt_commit(
                    '--force',
                    '--all', 
                    '--allow-empty',
                    '--message', '{msg}',
                    '--author', '{author_from_user(None)}') FROM dual;"""
        )
    return Branch.objects.get(name=name)


def get_or_make_merge_candidate(src, dest):
    mc = get_merge_candidate(src, dest)
    if not mc:
        mc = make_merge_candidate(src, dest)
    return mc


def _merge_candidate_name(src, dest):
    return f"xxx-merge-candidate--{src}--{dest}"


class MergeConflicts:
    """
    Must run under the mc branch
    """

    def __init__(self, src, dest):
        self.src = src
        self.dest = dest
        self.model_map = self._model_map()

    @staticmethod
    def _model_map():
        return {
            ct.model_class()._meta.db_table: ct.model_class()
            for ct in ContentType.objects.all()
        }

    def conflicts_exist(self):
        conflicts = Conflicts.objects.count() != 0
        violations = ConstraintViolations.objects.count() != 0
        return conflicts or violations

    def make_conflict_summary_table(self):
        conflicts = Conflicts.objects.all()
        violations = ConstraintViolations.objects.all()
        summary = {
            c.table: {
                "model": self._model_from_table(c.table),
                "num_conflicts": c.num_conflicts,
            }
            for c in conflicts
        }
        for v in violations:
            if v.table not in summary:
                summary[v.table] = {"model": self._model_from_table(v.table)}
            summary[v.table]["num_violations"] = v.num_violations
        return list(summary.values())

    def make_conflict_table(self):
        rows = []
        for c in Conflicts.objects.all():
            rows.extend(self.get_rows_level_conflicts(c, self.src, self.dest))
        return ConflictsTable(rows)

    def make_constraint_violations_table(self):
        rows = []
        for v in ConstraintViolations.objects.all():
            rows.extend(self.get_rows_level_violations(v))
        return ConstraintViolationsTable(rows)

    def get_rows_level_conflicts(self, conflict, src, dest):
        """
        todo
        """
        with connection.cursor() as cursor:
            # introspect table schema to query conflict data as json
            cursor.execute(f"DESCRIBE dolt_conflicts_{conflict.table}")
            fields = ",".join([f"'{tup[0]}', {tup[0]}" for tup in cursor.fetchall()])

            cursor.execute(
                f"""SELECT base_id, JSON_OBJECT({fields})
                    FROM dolt_conflicts_{conflict.table};"""
            )
            model_name = self._model_from_table(conflict.table)
            return [
                {
                    "model": model_name,
                    "id": self._object_name_from_id(conflict.table, tup[0]),
                    "conflicts": self._transform_conflicts_obj(tup[1]),
                }
                for tup in cursor.fetchall()
            ]

    def _transform_conflicts_obj(self, obj):
        if type(obj) is str:
            obj = json.loads(obj)
        obj2 = {}
        for k, v in obj.items():
            prefix = "our_"
            if not k.startswith(prefix):
                continue
            suffix = k[len(prefix) :]
            ours = obj[f"our_{suffix}"]
            theirs = obj[f"their_{suffix}"]
            base = obj[f"base_{suffix}"]

            conflicted = ours != theirs and ours != base
            if conflicted:
                obj2[suffix] = {
                    f"{self.dest}": ours,
                    f"{self.src}": theirs,
                    "base": base,
                }
        return obj2

    def get_rows_level_violations(self, violation):
        with connection.cursor() as cursor:
            rows = []
            model_name = self._model_from_table(violation.table)
            cursor.execute(
                f"""SELECT id, violation_type, violation_info
                    FROM dolt_constraint_violations_{violation.table};"""
            )
            for v_row in cursor.fetchall():
                obj_name = self._object_name_from_id(violation.table, v_row[0])
                rows.append(
                    {
                        "model": model_name,
                        "id": obj_name,
                        "violation_type": v_row[1],
                        "violations": self._fmt_violation(v_row, model_name, obj_name),
                    }
                )
            return rows

    def _model_from_table(self, tbl_name):
        model = self.model_map[tbl_name]
        return model._meta.verbose_name

    def _object_name_from_id(self, tbl_name, id):
        try:
            model = self.model_map[tbl_name]
            obj = model.objects.get(id=id)
            return str(obj)
        except ObjectDoesNotExist:
            return id

    def _fmt_violation(self, v_row, model_name, obj_name):
        v_type = v_row[1]
        v_info = json.loads(v_row[2])

        if v_type == "foreign key":
            if "ReferencedTable" in v_info:
                rt = v_info["ReferencedTable"]
                ref_model_name = self._model_from_table(rt)
                return f"""
                    The {model_name} "{obj_name}" references a 
                    missing "{ref_model_name}" object 
                """

        elif v_type == "unique index":
            if "Columns" in v_info:
                return f"""
                    The {model_name} "{obj_name}" violates a 
                    uniqueness constraint defined over the 
                    columns {v_info["Columns"]}
                """
        return "Unknown constraint violation"
