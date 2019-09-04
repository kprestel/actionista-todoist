# Copyright 2019, Rasmus Sorensen <rasmusscholer@gmail.com>
"""

Module containing Todoist action commands for the todoist-action-cli.




"""
import operator
import sys
import builtins

import dateparser
import parsedatetime
from dateutil import tz
from todoist.models import Item

from actionista import binary_operators
from actionista.date_utils import ISO_8601_FMT, start_of_day, DATE_DAY_FMT, end_of_day, local_time_to_utc
from actionista.todoist.tasks_utils import get_task_value, get_recurring_tasks
from actionista.todoist.tasks_utils import inject_tasks_date_fields, inject_tasks_project_fields


DEFAULT_TASK_PRINT_FMT = (
    "{project_name:15} "
    "{due_date_safe_dt:%Y-%m-%d %H:%M}  "
    "{priority_str} "
    "{checked_str} "
    "{content} "
    "(due: {due_string_safe!r})"
)
# Alternative print_fmt examples:
# print_fmt="{project_name:15} {due_date_safe_dt:%Y-%m-%d %H:%M  } {content}",
# print_fmt="{project_name:15} {due_date_safe_dt:%Y-%m-%d %H:%M}  {checked_str} {content}",
# print_fmt="{project_name:15} {due_date_safe_dt:%Y-%m-%d %H:%M}  {priority_str} {checked_str} {content}",


def print_tasks(
        tasks: list,
        print_fmt: str = DEFAULT_TASK_PRINT_FMT,
        header=None, sep: str = "\n",
        *,
        data_attr: str = "_custom_data",
        verbose: int = 0
):
    """ Print tasks, using a python format string.

    Examples:
        `-print`
        `-print "{project_name:15} {due_date_safe_dt:%Y-%m-%d %H:%M  } {content}"`
        `-print "{project_name:15} {content}" "Project name:   Task:`

    Args:
        tasks: List of tasks or task_data dicts to print.
        print_fmt: How to print each task.
            Note: You can use print_fmt="pprint" to just print all tasks using pprint.
        header: Print a header before printing the tasks.
        sep: How to separate each printed task. Default is just "\n".
        # Keyword only arguments:
        data_attr: The task attribute to get task data from.
            Original data from the Todoist Sync API is stored in Item.data,
            but I prefer to add derived data fields in a separate Item._custom_data,
            so that they don't get persisted when writing the cache to disk.
        verbose: The verbosity to print informational messages with during the filtering process.

    Returns: List of tasks.

    Frequently-used print formats:
        "* {content}"
        "{project_name:15} {due_date_safe_dt:%Y-%m-%d %H:%M  } {content}"
        "{project_name:15} {due_date_safe_iso}    {content}  ({checked})"
        "{project_name:15} {due_date_safe_dt:%Y-%m-%d %H:%M}  {checked}  {content}"
        "{project_name:15} {due_date_safe_dt:%Y-%m-%d %H:%M}  {checked_str}  {content}"

    Frequently-used headers:
        "Project:        Due_date:          Task:"
        "Project:        Due_date:          Done: Task:"
        "Project:        Due_date:        Done: Task:"
        "Project:        Due_date:        P:   Done: Task:"

    See `inject_tasks_date_fields()` for available date fields. In brief:
        due_date_utc
        due_date_utc_iso
        due_date_dt
        due_date_local_dt
        due_date_local_iso
        due_date_safe_dt   # These two '_safe_' fields are guaranteed to be present, even if the task
        due_date_safe_iso  # has no due_date, in which case we use the end of the century. (Note: local time!)
        # We also have the same above fields for `date_added` and `completed_date`.

    """
    if verbose > -1:
        print(f"\n - Printing {len(tasks)} tasks",
              f"separated by {sep!r}, using print_fmt:\n{print_fmt!r}.\n" if verbose else "...\n", file=sys.stderr)
    if header:
        print(header)
    # Use `task._custom_data`, which is a copy of task.data with extra stuff added (if available).
    task_dicts = [getattr(task, data_attr, task.data) if isinstance(task, Item) else task for task in tasks]
    if print_fmt == 'repr' or print_fmt == 'pprint':
        import pprint
        pprint.pprint(task_dicts)
    else:
        print(sep.join(print_fmt.format(task=task, **task) for task in task_dicts))
    return tasks


def sort_tasks(tasks, keys="project_name,priority_str,content", order="ascending",
               *, data_attr="_custom_data", verbose=0):
    """ Sort the list of tasks, by task attribute in ascending or descending order.

    Args:
        tasks: The tasks to sort (dicts or todoist.moddl.Item objects).
        keys: The keys to sort by. Should be a list or comma-separated string.
        order: The sort order, either ascending or descending.
        # Keyword only arguments:
        data_attr: Ues this attribute for task data. For instance, if the
        verbose: The verbosity to print informational messages with during the filtering process.

    Examples:

        Sort tasks by project_name, then priority, in descending order:
            -sort "project_name,priority" descending
            sort_tasks(tasks, keys="project_name,priority", order="descending")

    Frequently-used sortings:

        project_name,priority_str,item_order
        project_name,item_order
        due_date,priority,item_order

    """
    if verbose > -1:
        print(f"\n - Sorting {len(tasks)} tasks by {keys!r} ({order}).", file=sys.stderr)
    if isinstance(keys, str):
        keys = keys.split(',')
    itemgetter = operator.itemgetter(*keys)
    if data_attr:
        def keyfunc(task):
            return itemgetter(getattr(task, data_attr, task.data))
    else:
        keyfunc = itemgetter
    tasks = sorted(tasks, key=keyfunc, reverse=(order == "descending"))
    return tasks


def filter_tasks(
        tasks,
        taskkey, op_name, value,
        missing="exclude", default=None,
        value_transform=None, negate=False,
        data_attr="_custom_data",
        *, verbose=0):
    """ Generic task filtering method based on comparison with a specific task attribute.

    CLI signature:
        $ todoist-action-cli -filter <taskkey> <operator> <value> <missing> <default> <transform> <negate>
    e.g.
        $ todoist-action-cli -filter project_id eq 2076120802 exclude none int

    Args:
        tasks: List of tasks (dicts or todoist.Item).
        taskkey: The attribute key to compare against, e.g. 'due_date_local_iso' or 'project'.
        op_name: Name of the binary operator to use when comparing the task attribute against the given value.
        value: The value to compare the task's attribute against.
        missing: How to deal with tasks with missing attributes, e.g. "include", "exclude", or default to a given value.
        default: Use this value if a task attribute is missing and missing="default".
        value_transform: Perform a given transformation of the input value, e.g. `int`, or `str`.
            value_transform can be the name of any function/class in the current namespace.
        negate: Can be used to negate (invert) the filter.
            Some operators already have an inverse operator, e.g. `eq` vs `ne`, `le` vs `gt`.
            But other operators do not have a simple inverse operator, e.g. `startswith`.
            So, if you want to remove/exclude tasks starting with 'Email', use:
                -filter content startswith Email exclude _ _ True
            Note: Negate applies to the transform, but not to tasks included/excluded due to missing value.
        data_attr: Instead of using task or task.data, use `getatr(task, data_attr)`.
            This is useful if you are setting custom task data on `task._custom_data` to keep them separate.
        verbose: The verbosity to print informational messages with during the filtering process.

    Returns:
        Filtered list of tasks passing the filter criteria (attribute matching value).

    The `missing` parameter controls what to do if `taskkey` is not present in a task:
        "raise" -> raise a KeyError.
        "include" -> include the task (task passes filter evaluation).
        "exclude" -> exclude the task (task fails filter evaluation).
        "default" -> Use a default value in leiu of the missing value.

    What if task[taskkey] is None?
        * Since we can't really use None for any comparison, it should be considered as missing,
            exactly the same as if the key was missing.

    """
    # First, check values and print helpful warnings about frequent pitfalls:
    if op_name == 'le' and 'date' in taskkey and value[-2:] != '59':
        print("\nWARNING: You are using the less-than-or-equal-to (`le`) operator with a data value, "
              "which can be tricky. Consider using the less-than (`lt`) operator instead. If you do use the "
              "less-than-or-equal-to (`le`) operator, make sure to specify full time in comparison.\n")
    if taskkey == 'due_date_utc':
        print("\nNOTICE: You are using 'due_date_utc' as filter taskkey. This has the rather-unhelpful "
              "format: 'Mon 26 Mar 2018 21:59:59 +0000'.\n")
    # We often use "_" as placeholeder on the command line, because we cannot enter e None value:
    if default == '_' or default == '__None__':
        default = None
    if value_transform == '_' or value_transform == '__None__':
        value_transform = None
    if isinstance(negate, str) and negate.lower() in ('false', '_', '__none__', '0'):
        negate = False

    negate = bool(negate)
    op = getattr(binary_operators, op_name)
    # I'm currently negating explicitly in the all four 'missing' cases,
    # but I could also just have re-defined `op` as: `def op(a, b): return _op(a, b) != negate`

    if verbose > -1:
        print(f"\n - Filtering {len(tasks)} tasks with: {taskkey!r} {op_name} {value!r} "
              f"(missing={missing!r}, default={default!r}, value_transform={value_transform!r}, negate={negate!r}).",
              file=sys.stderr)

    if value_transform:
        if isinstance(value_transform, str):
            # It is either 'int' or a custom eval statement:
            if value_transform in builtins.__dict__:
                value_transform = getattr(builtins, value_transform)  # e.g. 'int'
                # print(f"Using `value_transform` {value_transform!r} from `builtins`...")
            elif value_transform in locals():
                value_transform = locals()[value_transform]
                # print(f"Using `value_transform` {value_transform!r} from `locals()`...")
            elif value_transform in globals():
                value_transform = globals()[value_transform]
                # print(f"Using `value_transform` {value_transform!r} from `globals()`...")
            else:
                if verbose > -1:
                    print(f"Creating filter value transform by `eval({value_transform!r})` ...")
                t = eval(value_transform)
                if hasattr(t, '__call__'):
                    value_transform = t
                else:
                    # Was a one-off eval transform intended as: `value = eval('value*2')`.
                    value = t
                    value_transform = None  # Prevent further transformation

    # value_transform can be used to transform the filter/comparison value to e.g. an int or datetime object:
    if value_transform:
        # custom callable:
        value = value_transform(value)
        if default and missing == "default":  # Only try to transform task default value if we actually need it
            default = value_transform(default)

    # TODO: Remove this! Instead, use `get_task_value()` - and `value_transform(value)` to tranform comparison value.
    def get_value(task, default_=None):
        nonlocal value
        task = getattr(task, data_attr) if isinstance(task, Item) else task
        # return taskkey not in task or op(itemgetter(task), value)
        if 'due' in task and taskkey in ('due_date', 'due_date_utc'):
            # Support for v7.1 Sync API with separate 'due' dict attribute:
            # Although this may depend on how `inject_tasks_date_fields()` deals with v7.1 tasks.
            due_dict = task.get('due') or {}
            task_value = due_dict.get(taskkey.replace('due_', ''))  # items in the 'due' dict don't have 'due_' prefix
        else:
            task_value = task.get(taskkey, default_)
        if task_value is not None and type(task_value) != type(value):
            # Note: We are converting the *comparison value*, not the task value:
            print("NOTICE: `type(task_value) != type(value)` - Coercing `value` to %s:" % type(task_value))
            value = type(task_value)(value)
        return task_value

    if missing == "raise":
        def filter_eval(task):
            task_value = get_value(task)
            if verbose > 0:
                print(f"\n - Evaluating: task[{taskkey!r}] = {task_value}  {op_name} ({op}) {value} "
                      f"for task {task['content']} (due: {get_task_value(task, 'due_date')}) ",
                      file=sys.stderr)
            if task_value is None:
                raise ValueError(f"Key {taskkey!r} not present (or None) in task {task['id']}: {task['content']}")
            return op(task_value, value) != negate  # This comparison with negate will negate if negate is True.
    elif missing == "include":
        def filter_eval(task):
            # return taskkey not in task or op(itemgetter(task), value)
            task_value = get_value(task)
            if verbose > 0:
                print(f"\n - Evaluating: task[{taskkey!r}] = {task_value}  {op_name} ({op}) {value} "
                      f"for task {task['content']} (due: {get_task_value(task, 'due_date')}) ",
                      file=sys.stderr)
            return task_value is None or (op(task_value, value) != negate)
    elif missing == "exclude":
        def filter_eval(task):
            # return taskkey in task and op(itemgetter(task), value)
            task_value = get_value(task)
            if verbose > 0:
                print(f"\n - Evaluating: task[{taskkey!r}] = {task_value}  {op_name} ({op}) {value} "
                      f"for task {task['content']} (due: {get_task_value(task, 'due_date')}) ",
                      file=sys.stderr)
            return task_value is not None and (op(task_value, value) != negate)
    elif missing == "default":
        def filter_eval(task):
            task_value = get_value(task, default)
            return op(task_value, value) != negate
        if default is None:
            print('\nWARNING: filter_tasks() called with missing="default" but no default value given (is None).\n')
    else:
        raise ValueError("Argument `missing` value %r not recognized." % (missing,))

    tasks = [task for task in tasks if filter_eval(task)]

    return tasks


def generic_args_filter_adaptor(tasks, taskkey, args, *, default_op='iglob', **kwargs):
    """ A generic adaptor for filter_tasks(), accepting custom *args list.

    Typical use case is to be able to process both of the following action requests
    with a single function:
        `-content RS123*` and `-content startswith RS123`.
    This adaptor function just uses the number of args given to determine if a
    binary operator was provided with the action request.

    Args:
        tasks: List of tasks, passed to filter_tasks.
        taskkey: The task attribute to filter on.
        default_op: The default binary operator to use, in the case the user did not specify one in args.
        *args: User-provided action args, e.g. ['RS123*"], or ['startswith', 'RS123']

    Returns:
        Filtered list of tasks.

    """
    assert len(args) >= 1
    if args[0] == 'not':
        negate = True
        args = args[1:]
    else:
        negate = False
    if len(args) == 1:
        # `-content RS123*`
        op_name, value, args = default_op, args[0], args[1:]
    else:
        # `-content startswith work`
        op_name, value, *args = args

    # print(f"generic_args_filter_adaptor: args={args}")  # debugging
    return filter_tasks(tasks, taskkey=taskkey, op_name=op_name, value=value, negate=negate, *args, **kwargs)


def special_is_filter(tasks, *args, **kwargs):
    """ Special -is filter for ad-hoc or frequently-used cases, e.g. `-is not checked`, etc.

    These are generally implemented on an as-needed basis.

    Args:
        tasks:
        *args:

    Returns:
        tasks: Filtered list of tasks.
    """
    if args[0] == 'not':
        negate = True
        args = args[1:]
    else:
        negate = False
    if args[0] == 'due' or args[0] == 'overdue':
        # Note: "-is due" is alias for "due today or overdue" which is equivalent to "due before tomorrow".
        if args[0:2] == ["due", "or", "overdue"] or args[0:2] == ["overdue", "or", "due"]:
            args[0:2] = ["due", "before", "tomorrow"]
        timefmt = ISO_8601_FMT
        taskkey = 'due_date_utc_iso'  # Switch to 'due_date_iso' (or 'due_date_dt')
        convert = None
        if args[0] == 'overdue':
            op_name = 'lt'
            convert = start_of_day
            when = "today"
        elif len(args) > 1:
            if args[1] in ('before', 'on', 'after'):
                when = args[2]
                if args[1] == 'before':
                    op_name = 'lt'
                    convert = start_of_day
                elif args[1] == 'on':
                    op_name = 'startswith'
                    timefmt = DATE_DAY_FMT
                else:  # args[2] == 'after':
                    op_name = 'gt'
                    convert = end_of_day
            else:
                # "-is due today", same as "-is due on today"
                when = args[1]
                op_name = 'startswith'
                timefmt = DATE_DAY_FMT
        else:
            # "-is due":
            when = "today"
            op_name = 'le'
            convert = end_of_day
        # Using dateparser.DateDataParser().get_date_data() instead of dateparser.parse() we get a 'period' indication:
        # date_data = dateparser.DateDataParser().get_date_data(when)
        # if date_data is None:
        #     raise ValueError("Could not parse due date %r" % (when,))
        # dt, accuracy = date_data['date_obj'], date_data['period']  # Max 'period' precision is 'day' :(
        # Using parsedatetime, since dateparser has a poor concept of accuracy:
        # parsedatetime also understands e.g. "in two days", etc.
        cal = parsedatetime.Calendar()
        # Note: cal.parse returns a time.struct_time, not datetime object,
        # use cal.parseDT() to get a datetime object. Or just dt = datetime.datetime(*dt[:6])
        dt, context = cal.parseDT(when, version=2)  # provide `version` to get a context obj.
        if not context.hasDate:
            raise ValueError("Could not parse due date %r" % (when,))
        if convert and not context.hasTime:
            # Only perform conversion, i.e. snap to start/end of day, when no time indication was provided:
            dt = convert(dt)
        utc_str = local_time_to_utc(dt, fmt=timefmt)
        # date_value_iso = dt.strftime(timefmt)
        # When we request tasks that are due, we don't want completed tasks, so remove these first:
        tasks = filter_tasks(tasks, taskkey="checked", op_name="eq", value=0, missing="include", **kwargs)
        # Then return tasks that are due as requested:
        # return filter_tasks(tasks, taskkey=taskkey, op_name=op_name, value=utc_str, negate=negate, **kwargs)
        # Update, 2019-Sep: Use local datetime object for comparison:
        # OBS: can't compare offset-naive and offset-aware datetimes - so make sure `dt` has tzinfo:
        dt = dt.astimezone(tz.tzlocal())
        # Discussion: Maybe use 'due_date_safe_dt', which where tasks with no due date is set to a distant future.
        return filter_tasks(tasks, taskkey='due_date_dt', op_name=op_name, value=dt, negate=negate,
                            missing='exclude', **kwargs)
    elif args[0] in ('checked', 'unchecked', 'complete', 'incomplete', 'completed', 'done'):
        # -is not checked
        if args[0][:2] in ('in', 'un'):
            checked_val = 0
        else:
            checked_val = 1
        taskkey = "checked"
        op_name = "eq"
        return filter_tasks(tasks, taskkey=taskkey, op_name=op_name, value=checked_val, negate=negate, **kwargs)
    elif args[0] == 'in':
        taskkey = "project_name"
        op_name = "eq"
        value = args[1]
        return filter_tasks(tasks, taskkey=taskkey, op_name=op_name, value=value, negate=negate, **kwargs)
    elif args[0] == 'recurring':
        """ `-is [not] recurring` filter.
        NOTE: The 'is_recurring' attribute is not officially exposed and "may be removed soon",
        c.f. https://github.com/Doist/todoist-api/issues/33.
        Until then, perhaps it is better to filter based on whether the due_string starts with the word "every". 
        """
        # taskkey = "is_recurring"
        # op_name = "eq"
        # value = 1
        # return filter_tasks(tasks, taskkey=taskkey, op_name=op_name, value=value, negate=negate)
        # -is not recurring : for recurring task : negate==True, startswith('every')==True => startswith == negate
        print(f"\n - Filtering {len(tasks)} tasks, excluding {'' if negate else 'non-'}recurring tasks...")
        return get_recurring_tasks(tasks, negate=negate)
    else:
        raise ValueError("`-is` parameter %r not recognized. (args = %r)" % (args[0], args))


def is_not_filter(tasks, *args, **kwargs):
    """ Convenience `-not` action, just an alias for `-is not`. Can be used as e.g. `-not recurring`."""
    args = ['not'] + list(args)
    return special_is_filter(tasks, *args, **kwargs)


def due_date_filter(tasks, *when, **kwargs):
    """ Special `-due [when]` filter. Is just an alias for `-is due [when]`. """
    args = ['due'] + list(when)  # Apparently *args is a tuple, not a list.
    return special_is_filter(tasks, *args, **kwargs)


def content_filter(tasks, *args, **kwargs):
    """ Convenience adaptor to filter tasks based on the 'content' attribute (default op_name 'iglob'). """
    return generic_args_filter_adaptor(tasks=tasks, taskkey='content', args=args, **kwargs)


def content_contains_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="content", op_name="contains". """
    return filter_tasks(tasks, taskkey="content", op_name="contains", value=value, *args, **kwargs)


def content_startswith_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="content", op_name="startswith". """
    return filter_tasks(tasks, taskkey="content", op_name="startswith", value=value, *args, **kwargs)


def content_endswith_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="content", op_name="endswith"."""
    return filter_tasks(tasks, taskkey="content", op_name="endswith", value=value, *args, **kwargs)


def content_glob_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="content", op_name="glob". """
    return filter_tasks(tasks, taskkey="content", op_name="glob", value=value, *args, **kwargs)


def content_iglob_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="content", op_name="iglob". """
    return filter_tasks(tasks, taskkey="content", op_name="iglob", value=value, *args, **kwargs)


def content_eq_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="content", op_name="eq". """
    return filter_tasks(tasks, taskkey="content", op_name="eq", value=value, *args, **kwargs)


def content_ieq_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="content", op_name="ieq". """
    return filter_tasks(tasks, taskkey="content", op_name="ieq", value=value, *args, **kwargs)


def project_filter(tasks, *args, **kwargs):
    """ Convenience adaptor for filter action using taskkey="project_name" (default op_name "iglob"). """
    return generic_args_filter_adaptor(tasks=tasks, taskkey='project_name', args=args, **kwargs)


def project_iglob_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="content", op_name="iglob". """
    return filter_tasks(tasks, taskkey="project_name", op_name="iglob", value=value, *args, **kwargs)


def priority_filter(tasks, *args, **kwargs):
    """ Convenience adaptor for filter action using taskkey="priority" (default op_name "eq"). """
    return generic_args_filter_adaptor(
        tasks=tasks, taskkey='priority', args=args, default_op='eq', value_transform=int, **kwargs)


def priority_ge_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="priority", op_name="ge". """
    value = int(value)
    return filter_tasks(tasks, taskkey="priority", op_name="ge", value=value, *args, **kwargs)


def priority_eq_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="priority", op_name="eq". """
    value = int(value)
    return filter_tasks(tasks, taskkey="priority", op_name="eq", value=value, *args, **kwargs)


def priority_str_filter(tasks, *args, **kwargs):
    """ Convenience adaptor for filter action using taskkey="priority_str" (default op_name "eq"). """
    # return filter_tasks(tasks, taskkey="priority_str", op_name="eq", value=value, *args)
    return generic_args_filter_adaptor(
        tasks=tasks, taskkey='priority_str', args=args, default_op='eq', **kwargs
    )


def priority_str_eq_filter(tasks, value, *args, **kwargs):
    """ Convenience filter action using taskkey="priority_str", op_name="eq". """
    return filter_tasks(tasks, taskkey="priority_str", op_name="eq", value=value, *args, **kwargs)


def p1_filter(tasks, *args, **kwargs):
    """ Filter tasks including only tasks with priority 'p1'. """
    return priority_str_eq_filter(tasks, value="p1", *args, **kwargs)


def p2_filter(tasks, *args, **kwargs):
    """ Filter tasks including only tasks with priority 'p2'. """
    return priority_str_eq_filter(tasks, value="p2", *args, **kwargs)


def p3_filter(tasks, *args, **kwargs):
    """ Filter tasks including only tasks with priority 'p3'. """
    return priority_str_eq_filter(tasks, value="p3", *args, **kwargs)


def p4_filter(tasks, *args, **kwargs):
    """ Filter tasks including only tasks with priority 'p3'. """
    return priority_str_eq_filter(tasks, value="p4", *args, **kwargs)


def reschedule_tasks(
        tasks, new_date, timezone='date_string', update_local=False, check_recurring=True, *,
        verbose=0
):
    """ Reschedule tasks to a new date/time.

    Example: Reschedule overdue tasks for tomorrow
        $ todoist-action-cli -sync -due before today -reschedule tomorrow
    Will reschedule overdue tasks using:
        reschedule_tasks(tasks, 'tomorrow')

    Args:
        tasks: List of tasks.
        new_date: The new due_date string to send.
        timezone: The timezone to use.
            Special case `timezone='date_string' (default) means that instead of
            updating the due_date_utc, just send `date_string` to the Todoist server.
        update_local: Update the local tasks 'due_date_utc' attribute, and then pass the tasks through
            inject_tasks_date_fields(), which will update all other date-related attributes.
        check_recurring: If True, will check whether the task list contains recurring tasks,
            and print a warning if it does. Rescheduling a recurring task may be problematic,
            as it will cause it to not be recurring anymore.
        verbose: Adjust the verbosity to increase or decrease the amount of information printed during function run.

    Returns:
        List of tasks.

    WOOOOOT:
    When SENDING an updated `due_date_utc`, it must be in ISO8601 format!
    From https://developer.todoist.com/sync/v7/?shell#update-an-item :
    > The date of the task in the format YYYY-MM-DDTHH:MM (for example: 2012-3-24T23:59).
    > The value of due_date_utc must be in UTC. Note that, when the due_date_utc argument is specified,
    > the date_string is required and has to specified as well, and also, the date_string argument will be
    > parsed as local timestamp, and converted to UTC internally, according to the user’s profile settings.

    Maybe take a look at what happens in the web-app when you reschedule a task?
    Hmm, the webapp uses the v7.1 Sync API at /API/v7.1/sync.
    The v7.1 API uses task items with a "due" dict with keys "date", "timezone", "is_recurring", "string", and "lang".
    This seems to make 'due_date_utc' obsolete. Seems like a good decision, but it makes some of my work obsolete.

    Perhaps it is easier to just only pass `date_string`, especially for non-recurring tasks.

    Regarding v7.1 Sync API:
        * The web client doesn't send "next sunday" date strings any more. The client is in charge of parsing
            the date and sending a valid date. The due.string was set to "15 Apr".

    # TODO: This should be updated for v8 Sync API:
    # TODO: * Perhaps make two separate commands, `-reschedule-recurring` and `-reschedule-nonrecurring`.
    # TODO: * These functions can deal with the situations where you would like to force a specific behavior,
    # TODO:   e.g. make a recurring task non-recurring, or vice-versa.
    # TODO: * This `-reschedule` task is then still in charge of rescheduling tasks,
    # TODO:   but it can use a default behavior:
    # TODO: * For regular, non-recurring tasks: update due.date and due.string.
    # TODO:   (or is it OK to just specify due.string like you currently do?)
    # TODO: * For recurring tasks, update due.date, and leave due.string at its current value.
    # TODO: * In order to specify due.date as e.g. "2019-09-05" (without time specifier),
    # TODO:   you probably have to use `parsedatetime` instead of `dateparser`.

    """
    if verbose > -1:
        print("\n - Rescheduling %s tasks for %r..." % (len(tasks), new_date), file=sys.stderr)
        print(" - Remember to use `-commit` to push the changes (not `-sync`)!\n\n", file=sys.stderr)
    if check_recurring is True:
        recurring_tasks = get_recurring_tasks(tasks)
        if len(recurring_tasks) > 0:
            print("\nWARNING: One or more of the tasks being rescheduled is recurring:")
            print_tasks(recurring_tasks)
            print("\n")
    if timezone == 'date_string':  # Making this the default
        # Special case; instead of updating the due_date_utc, just send `date_string` to server.
        # Note: For non-repeating tasks, this is certainly by far the simplest way to update due dates.
        for task in tasks:
            if 'due' in task.data:
                # Support v8 Sync API with dedicated 'due' dict attribute.
                new_due = {'string': new_date}
                task.update(due=new_due)
            else:
                task.update(date_string=new_date)
        return tasks
    if isinstance(new_date, str):
        new_date_str = new_date  # Save the str
        new_date = dateparser.parse(new_date)
        if new_date is None:
            raise ValueError("Could not parse date %r." % (new_date_str,))
        # Hmm, dateparser.parse evaluates "tomorrow" as "24 hours from now", not "tomorrow at 0:00:00).
        # This is problematic since we typically reschedule tasks as all day events.
        # dateparser has 'tomorrow' hard-coded as alias for "in 1 day", making it hard to re-work.
        # Maybe it is better to just reschedule using date_string?
        # But using date_string may overwrite recurring tasks?
        # For now, just re-set time manually if new_date_str is "today", "tomorrow", etc.
        if new_date_str in ('today', 'tomorrow') or 'days' in new_date_str:
            new_date = new_date.replace(hour=23, minute=59, second=59)  # The second is the important part for Todoist.
        # For more advanced, either use a different date parsing library, or use pendulum to shift the date.
        # Alternatively, use e.g. parsedatetime, which supports "eod tomorrow".
    if new_date.tzinfo is None:
        if timezone == 'local':
            timezone = tz.tzlocal()
        elif isinstance(timezone, str):
            timezone = tz.gettz(timezone)
        new_date.replace(tzinfo=timezone)
    # Surprisingly, when adding or updating due_date_utc with the v7.0 Sync API,
    # `due_date_utc` should supposedly be in ISO8601 format, not the usual ctime-like format. Sigh.
    new_date_utc = local_time_to_utc(new_date, fmt=ISO_8601_FMT)
    for task in tasks:
        date_string = task['date_string']
        task.update(due_date_utc=new_date_utc, date_string=date_string)
        # Note: other fields are not updated!
        if update_local:
            task['due_date_utc'] = new_date_utc
    if update_local:
        inject_tasks_date_fields(tasks)
    return tasks


def update_tasks(tasks, *, verbose=0, **kwargs):
    """ Generic task updater. (NOT FUNCTIONAL)

    Todoist task updating caveats:

    * priority: This is VERY weird! From the v7 sync API docs:
        > "Note: Keep in mind that "very urgent" is the priority 1 on clients. So, p1 will return 4 in the API."
        In other words, these are all reversed:
            p4 -> 1, p3 -> 2, p2 -> 3, p1 -> 4.
        That is just insane.

    """
    if verbose > -1:
        print("Updating tasks using kwargs:", kwargs)
    for task in tasks:
        task.update(**kwargs)
    return tasks


def mark_tasks_completed(tasks, method='close', *, verbose=0):
    """ Mark tasks as completed using method='close'.

    Note: The Todoist v7 Sync API has two command types for completing tasks:
        'item_complete' - used by ?
        'item_update_date_complete' - used to mark recurring task completion.
        'item_close' - does exactly what official clients do when you close a task:
            regular task is completed and moved to history,
            subtasks are checked (marked as done, but not moved to history),
            recurring task is moved forward (due date is updated).
            Aka: "done".

    See: https://developer.todoist.com/sync/v7/#complete-items

    Args:
        tasks:  List of tasks.
        method: The method used to close the task.
            There are several meanings of marking a task as completed, especially for recurring tasks.
        verbose: Increase or decrease the verbosity of the information printed during function run.

    Returns:
        tasks:  List of tasks (after closing them).

    See also:
        'item_uncomplete' - Mark task as uncomplete (re-open it).

    """
    if verbose > 0:
        print(f"\nMarking tasks as complete using method {method!r}...")
        if method in ('close', 'item_close'):
            print(f"\nOBS: Consider using `-close` command directly instead ...")
        print(" --> Remember to `-commit` the changes to the server! <--")

    for task in tasks:
        if method in ('close', 'item_close'):
            task.close()
        elif method in ('complete', 'item_complete'):
            task.complete()
        elif method in ('item_update_date_complete', 'complete_recurring'):
            raise NotImplementedError(
                f"Using method {method!r} is not implemented. "
                f"If using the CLI, please use the `-close` action instead. "
                f"If calling from Python, please use either close_tasks() function, or "
                f"`task.update_date_complete()`. ")
        else:
            raise ValueError(f"Value for method = {method!r} not recognized!")
    return tasks


def close_tasks(tasks, *, verbose=0):
    """ Mark tasks as completed using method='close'.

    See mark_tasks_completed for more info on the different API methods to "complete" a task.

    See: https://developer.todoist.com/sync/v8/#close-item

    Args:
        tasks:  List of tasks.
        verbose: Increase or decrease the verbosity of the information printed during function run.

    Returns:
        tasks:  List of tasks (after closing them).

    See also:
        'item_uncomplete' - Mark task as uncomplete (re-open it).

    """
    if verbose > 0:
        print(f"\nClosing tasks (using API method 'item_close') ...")
        print(" --> Remember to `-commit` the changes to the server! <--")
    for task in tasks:
        task.close()
    return tasks


def complete_and_update_date_for_recurring_tasks(tasks, new_date=None, due_string=None, *, verbose=0):
    """ Mark tasks as completed using method='item_update_date_complete'.

    See mark_tasks_completed for more info on the different API methods to "complete" a task.

    See: https://developer.todoist.com/sync/v8/#close-item

    Args:
        tasks:  List of tasks.
        new_date: The new/next due/occurrence date for the recurring task,
            e.g. `new_date="2019-09-
        due_string: Change the "due string" that specifies when the task occurs,
            e.g. `due_string="every monday 5 pm"`.
        verbose: Increase or decrease the verbosity of the information printed during function run.

    Returns:
        tasks:  List of tasks (after completing/updating them).

    See also:
        'item_update_date_complete' - Mark task as uncomplete (re-open it).

    """
    if verbose > 0:
        print(f"\nCompleting recurring tasks and moving the due date to {new_date if new_date else 'next occurrence'} "
              f"(using API method 'item_update_date_complete') ...")
        print(" --> Remember to `-commit` the changes to the server! <--")
    print("NOTICE: todoist.models.Item.update_date_complete() is currently broken in "
          "todoist-python package version 8.0.0.")
    for task in tasks:
        task.update_date_complete(new_date, due_string)
    return tasks


def uncomplete_tasks(tasks, *, verbose=0):
    """ Re-open tasks (uncomplete tasks) using API method 'item_uncomplete'.

    See: https://developer.todoist.com/sync/v8/#uncomplete-item

    Args:
        tasks:  List of tasks.
        verbose: Increase or decrease the verbosity of the information printed during function run.

    Returns:
        tasks:  List of tasks (after reopening them).

    """
    if verbose > 0:
        print(f"\nRe-opening tasks (using API method 'item_uncomplete') ...")
        print(" --> Remember to `-commit` the changes to the server! <--")
    for task in tasks:
        task.uncomplete()
    return tasks


def archive_tasks(tasks, *, verbose=0):
    """ Archive tasks using API method 'item_archive'.

    See: https://developer.todoist.com/sync/v8/?shell#archive-item

    Args:
        tasks:  List of tasks.
        verbose: Increase or decrease the verbosity of the information printed during function run.

    Returns:
        tasks:  List of tasks (after archiving them).

    """
    if verbose > 0:
        print(f"\nArchiving tasks (using API method 'item_archive') ...")
        print(" --> Remember to `-commit` the changes to the server! <--")
    for task in tasks:
        task.archive()
    return tasks


def delete_tasks(tasks, *, verbose=0):
    """ Delete tasks using API method 'item_delete'.

    See: https://developer.todoist.com/sync/v8/?shell#delete-item

    Args:
        tasks:  List of tasks.
        verbose: Increase or decrease the verbosity of the information printed during function run.

    Returns:
        tasks:  List of tasks (after deleting them).

    """
    if verbose > 0:
        print(f"\nArchiving tasks (using API method 'item_delete') ...")
        print(" --> Remember to `-commit` the changes to the server! <--")
    for task in tasks:
        task.delete()
    return tasks


def fetch_completed_tasks(tasks, *, verbose=0):
    """ This will replace `tasks` with a list of completed tasks dicts. May not work nicely. Only for playing around.

    You should probably use the old CLI instead:
        $ todoist print-completed-today --print-fmt "* {title}"
    """
    if verbose > -1:
        print(f"Discarting the current {len(tasks)} tasks, and fetching completed tasks instead...")
    from actionista.todoist.adhoc_cli import completed_get_all
    # This will use a different `api` object instance to fetch completed tasks:
    tasks, projects = completed_get_all()
    inject_tasks_project_fields(tasks, projects)
    return tasks


# Defined ACTIONS dict AFTER we define the functions.
# OBS: ALL action functions MUST return tasks, never None.

ACTIONS = {
    'print': print_tasks,
    'sort': sort_tasks,
    'filter': filter_tasks,
    'has': filter_tasks,  # Undocumented alias, for now.
    'is': special_is_filter,  # Special cases, e.g. "-is incomplete" or "-is not overdue".
    'not': is_not_filter,
    'due': due_date_filter,
    # contains, startswith, glob/iglob, eq/ieq are all trivial derivatives of filter:
    # But they are special in that we use the binary operator name as the action name,
    # and assumes we want to filter the tasks, using 'content' as the task key/attribute.
    'contains': content_contains_filter,
    'startswith': content_startswith_filter,
    'endswith': content_endswith_filter,
    'glob': content_glob_filter,
    'iglob': content_iglob_filter,
    'eq': content_eq_filter,
    'ieq': content_ieq_filter,
    # Convenience actions where action name specifies the task attribute to filter on:
    'content': content_filter,  # `-content endswith "sugar".
    'name': content_filter,  # Alias for content_filter.
    'project': project_filter,
    'priority': priority_filter,
    # More derived 'priority' filters:
    'priority-eq': priority_eq_filter,
    'priority-ge': priority_ge_filter,
    'priority-str': priority_str_filter,
    'priority-str-eq': priority_str_eq_filter,
    'p1': p1_filter,
    'p2': p2_filter,
    'p3': p3_filter,
    'p4': p4_filter,
    # Update task actions:
    'reschedule': reschedule_tasks,
    'mark-completed': mark_tasks_completed,
    # 'mark-as-done': mark_tasks_completed,  # Deprecated.
    'close': close_tasks,
    'reopen': uncomplete_tasks,  # alias for uncomplete
    'uncomplete': uncomplete_tasks,
    'archive': archive_tasks,
    'complete_and_update': complete_and_update_date_for_recurring_tasks,
    # 'fetch-completed': fetch_completed_tasks,  # WARNING: Not sure how this works, but probably doesn't work well.
    # The following actions are overwritten when the api object is created inside the action_cli() function:
    'verbose': None, 'v': None,  # Increase verbosity.
    'delete-cache': None,  # Delete local cache files.
    'sync': None,  # Pulls updates from the server, but does not push changes to the server.
    'commit': None,  # Push changes to the server.
    'show-queue': None,  # Show the command queue, that will be pushed to the server on `commit`.
}