import React, { useMemo, useState } from "react";
import {
    Accordion,
    AccordionDetails,
    AccordionSummary,
    Box,
    Button,
    Chip,
    CircularProgress,
    Divider,
    FormControlLabel,
    Grid,
    Stack,
    Switch,
    TextField,
    ToggleButton,
    ToggleButtonGroup,
    Tooltip,
    Typography,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import RefreshIcon from "@mui/icons-material/Refresh";
import { useSnackbar } from "material-ui-snackbar-provider";
import {
    graphql,
    useLazyLoadQuery,
    useMutation,
    fetchQuery,
    useRelayEnvironment,
} from "react-relay";

import Loader from "./Loader";
import { AdminPanelQuery } from "./__generated__/AdminPanelQuery.graphql";
import { AdminPanelTriggerMutation } from "./__generated__/AdminPanelTriggerMutation.graphql";
import { AdminPanelPauseMutation } from "./__generated__/AdminPanelPauseMutation.graphql";
import { AdminPanelResumeMutation } from "./__generated__/AdminPanelResumeMutation.graphql";
import { AdminPanelUpdateTriggerMutation } from "./__generated__/AdminPanelUpdateTriggerMutation.graphql";

const AdminQueryNode = graphql`
    query AdminPanelQuery {
        scheduledJobs {
            id
            name
            description
            nextRunTime
            paused
            trigger {
                kind
                expression
                fields
            }
        }
    }
`;

const TriggerMutation = graphql`
    mutation AdminPanelTriggerMutation($id: ID!) {
        triggerScheduledJob(id: $id) {
            id
            nextRunTime
            paused
        }
    }
`;

const PauseMutation = graphql`
    mutation AdminPanelPauseMutation($id: ID!) {
        pauseScheduledJob(id: $id) {
            id
            nextRunTime
            paused
        }
    }
`;

const ResumeMutation = graphql`
    mutation AdminPanelResumeMutation($id: ID!) {
        resumeScheduledJob(id: $id) {
            id
            nextRunTime
            paused
        }
    }
`;

const UpdateTriggerMutation = graphql`
    mutation AdminPanelUpdateTriggerMutation(
        $input: UpdateScheduledJobTriggerInput!
    ) {
        updateScheduledJobTrigger(data: $input) {
            id
            nextRunTime
            paused
            trigger {
                kind
                expression
                fields
            }
        }
    }
`;

type Job = AdminPanelQuery["response"]["scheduledJobs"][number];

const formatNextRun = (iso: string | null | undefined): string => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
};

interface CronFields {
    year: string;
    month: string;
    day: string;
    week: string;
    day_of_week: string;
    hour: string;
    minute: string;
    second: string;
}

interface IntervalFields {
    weeks: string;
    days: string;
    hours: string;
    minutes: string;
    seconds: string;
}

const blankCron: CronFields = {
    year: "",
    month: "",
    day: "",
    week: "",
    day_of_week: "",
    hour: "",
    minute: "",
    second: "",
};

const blankInterval: IntervalFields = {
    weeks: "",
    days: "",
    hours: "",
    minutes: "",
    seconds: "",
};

const cronFromJobFields = (
    fields: Record<string, unknown> | null | undefined
): CronFields => {
    const f = (fields ?? {}) as Record<string, string>;
    return {
        ...blankCron,
        ...Object.fromEntries(
            Object.entries(f).filter(([, v]) => typeof v === "string")
        ),
    } as CronFields;
};

const intervalFromJobFields = (
    fields: Record<string, unknown> | null | undefined
): IntervalFields => {
    const f = (fields ?? {}) as Record<string, string>;
    const result: IntervalFields = { ...blankInterval };
    (["weeks", "days", "hours", "minutes", "seconds"] as const).forEach((k) => {
        const v = f[k];
        if (v && v !== "0") result[k] = v;
    });
    return result;
};

const JobRow: React.FC<{ job: Job; onAfterMutation: () => void }> = ({
    job,
    onAfterMutation,
}) => {
    const snackbar = useSnackbar();
    const [trigger, isTriggering] =
        useMutation<AdminPanelTriggerMutation>(TriggerMutation);
    const [pause, isPausing] =
        useMutation<AdminPanelPauseMutation>(PauseMutation);
    const [resume, isResuming] =
        useMutation<AdminPanelResumeMutation>(ResumeMutation);
    const [updateTrigger, isUpdating] =
        useMutation<AdminPanelUpdateTriggerMutation>(UpdateTriggerMutation);

    const initialKind = job.trigger.kind === "interval" ? "interval" : "cron";
    const [kind, setKind] = useState<"cron" | "interval">(initialKind);
    const [cronFields, setCronFields] = useState<CronFields>(() =>
        job.trigger.kind === "cron"
            ? cronFromJobFields(
                  job.trigger.fields as Record<string, unknown> | null
              )
            : blankCron
    );
    const [intervalFields, setIntervalFields] = useState<IntervalFields>(() =>
        job.trigger.kind === "interval"
            ? intervalFromJobFields(
                  job.trigger.fields as Record<string, unknown> | null
              )
            : blankInterval
    );

    const handleTrigger = () => {
        trigger({
            variables: { id: job.id },
            onCompleted: () => {
                snackbar.showMessage(`${job.name}: scheduled to run now`);
                onAfterMutation();
            },
            onError: (e) =>
                snackbar.showMessage(`Trigger failed: ${e.message}`),
        });
    };

    const handleToggleEnabled = () => {
        const fn = job.paused ? resume : pause;
        fn({
            variables: { id: job.id },
            onCompleted: () => {
                snackbar.showMessage(
                    `${job.name}: ${job.paused ? "resumed" : "paused"}`
                );
                onAfterMutation();
            },
            onError: (e) => snackbar.showMessage(`Toggle failed: ${e.message}`),
        });
    };

    const handleSaveSchedule = () => {
        const cron =
            kind === "cron"
                ? Object.fromEntries(
                      Object.entries(cronFields).filter(([, v]) => v !== "")
                  )
                : null;
        const interval =
            kind === "interval"
                ? Object.fromEntries(
                      Object.entries(intervalFields)
                          .filter(([, v]) => v !== "")
                          .map(([k, v]) => [k, parseInt(v, 10)])
                  )
                : null;
        updateTrigger({
            variables: {
                input: {
                    id: job.id,
                    kind,
                    cron: cron && Object.keys(cron).length ? cron : null,
                    interval:
                        interval && Object.keys(interval).length
                            ? interval
                            : null,
                },
            },
            onCompleted: () => {
                snackbar.showMessage(`${job.name}: schedule updated`);
                onAfterMutation();
            },
            onError: (e) => snackbar.showMessage(`Update failed: ${e.message}`),
        });
    };

    const busy = isTriggering || isPausing || isResuming || isUpdating;

    return (
        <Accordion disableGutters>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                <Box
                    sx={{
                        display: "grid",
                        gridTemplateColumns: "auto 2fr 1fr 1fr auto",
                        alignItems: "center",
                        width: "100%",
                        gap: 2,
                    }}
                    onClick={(e) => e.stopPropagation()}
                >
                    <FormControlLabel
                        sx={{ ml: 0 }}
                        control={
                            <Switch
                                checked={!job.paused}
                                onChange={handleToggleEnabled}
                                disabled={busy}
                            />
                        }
                        label=""
                    />
                    <Box sx={{ minWidth: 0 }}>
                        <Typography variant="subtitle1" noWrap>
                            {job.name}
                        </Typography>
                        <Typography
                            variant="body2"
                            color="text.secondary"
                            noWrap
                        >
                            {job.description}
                        </Typography>
                    </Box>
                    <Box>
                        <Chip
                            size="small"
                            label={job.trigger.kind}
                            color={
                                job.trigger.kind === "cron"
                                    ? "primary"
                                    : "default"
                            }
                            sx={{ mr: 1 }}
                        />
                        <Typography
                            component="code"
                            variant="caption"
                            sx={{ fontFamily: "monospace" }}
                        >
                            {job.trigger.expression}
                        </Typography>
                    </Box>
                    <Box>
                        <Typography variant="caption" color="text.secondary">
                            Next run
                        </Typography>
                        <Typography variant="body2">
                            {job.paused ? "—" : formatNextRun(job.nextRunTime)}
                        </Typography>
                    </Box>
                    <Tooltip title="Trigger this job to run on the next scheduler tick">
                        <span>
                            <Button
                                variant="outlined"
                                size="small"
                                startIcon={
                                    isTriggering ? (
                                        <CircularProgress size={14} />
                                    ) : (
                                        <PlayArrowIcon />
                                    )
                                }
                                onClick={handleTrigger}
                                disabled={busy}
                            >
                                Trigger now
                            </Button>
                        </span>
                    </Tooltip>
                </Box>
            </AccordionSummary>
            <AccordionDetails>
                <Stack spacing={2}>
                    <Box>
                        <Typography variant="overline">Trigger type</Typography>
                        <Box>
                            <ToggleButtonGroup
                                value={kind}
                                exclusive
                                size="small"
                                onChange={(_, v) =>
                                    v && setKind(v as "cron" | "interval")
                                }
                            >
                                <ToggleButton value="cron">Cron</ToggleButton>
                                <ToggleButton value="interval">
                                    Interval
                                </ToggleButton>
                            </ToggleButtonGroup>
                        </Box>
                    </Box>

                    {kind === "cron" ? (
                        <Box>
                            <Typography variant="overline">
                                Cron schedule (blank = wildcard)
                            </Typography>
                            <Grid container spacing={1}>
                                {(
                                    [
                                        ["day_of_week", "Day of week"],
                                        ["hour", "Hour"],
                                        ["minute", "Minute"],
                                        ["second", "Second"],
                                        ["day", "Day of month"],
                                        ["month", "Month"],
                                        ["week", "Week"],
                                        ["year", "Year"],
                                    ] as [keyof CronFields, string][]
                                ).map(([key, label]) => (
                                    <Grid item xs={6} sm={3} key={key}>
                                        <TextField
                                            size="small"
                                            fullWidth
                                            label={label}
                                            placeholder="*"
                                            value={cronFields[key]}
                                            onChange={(e) =>
                                                setCronFields({
                                                    ...cronFields,
                                                    [key]: e.target.value,
                                                })
                                            }
                                        />
                                    </Grid>
                                ))}
                            </Grid>
                        </Box>
                    ) : (
                        <Box>
                            <Typography variant="overline">
                                Interval (blank = 0)
                            </Typography>
                            <Grid container spacing={1}>
                                {(
                                    [
                                        ["weeks", "Weeks"],
                                        ["days", "Days"],
                                        ["hours", "Hours"],
                                        ["minutes", "Minutes"],
                                        ["seconds", "Seconds"],
                                    ] as [keyof IntervalFields, string][]
                                ).map(([key, label]) => (
                                    <Grid item xs={6} sm={2.4} key={key}>
                                        <TextField
                                            size="small"
                                            fullWidth
                                            type="number"
                                            label={label}
                                            inputProps={{ min: 0 }}
                                            value={intervalFields[key]}
                                            onChange={(e) =>
                                                setIntervalFields({
                                                    ...intervalFields,
                                                    [key]: e.target.value,
                                                })
                                            }
                                        />
                                    </Grid>
                                ))}
                            </Grid>
                        </Box>
                    )}

                    <Divider />

                    <Stack
                        direction="row"
                        justifyContent="space-between"
                        alignItems="center"
                    >
                        <Typography
                            variant="caption"
                            color="text.secondary"
                            sx={{ fontFamily: "monospace" }}
                        >
                            id: {job.id}
                        </Typography>
                        <Button
                            variant="contained"
                            size="small"
                            onClick={handleSaveSchedule}
                            disabled={busy}
                        >
                            Save schedule
                        </Button>
                    </Stack>
                </Stack>
            </AccordionDetails>
        </Accordion>
    );
};

const AdminPanelInner: React.FC<{
    refetchKey: number;
    refetch: () => void;
}> = ({ refetchKey, refetch }) => {
    const data = useLazyLoadQuery<AdminPanelQuery>(
        AdminQueryNode,
        {},
        { fetchKey: refetchKey, fetchPolicy: "network-only" }
    );

    const jobs = useMemo(() => [...data.scheduledJobs], [data]);
    const enabledCount = jobs.filter((j) => !j.paused).length;
    const pausedCount = jobs.length - enabledCount;

    return (
        <Box sx={{ p: 3 }}>
            <Stack
                direction="row"
                justifyContent="space-between"
                alignItems="center"
                sx={{ mb: 2 }}
            >
                <Box>
                    <Typography variant="h4">Scheduled jobs</Typography>
                    <Typography variant="body2" color="text.secondary">
                        Background tasks run by APScheduler. Edit the trigger,
                        pause, or run on demand.
                    </Typography>
                </Box>
                <Button
                    variant="outlined"
                    startIcon={<RefreshIcon />}
                    onClick={refetch}
                >
                    Refresh
                </Button>
            </Stack>

            <Stack direction="row" spacing={4} sx={{ mb: 3 }}>
                <Box>
                    <Typography variant="h5">{jobs.length}</Typography>
                    <Typography variant="caption" color="text.secondary">
                        jobs
                    </Typography>
                </Box>
                <Box>
                    <Typography variant="h5" color="success.main">
                        {enabledCount}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                        enabled
                    </Typography>
                </Box>
                <Box>
                    <Typography
                        variant="h5"
                        color={
                            pausedCount > 0 ? "warning.main" : "text.primary"
                        }
                    >
                        {pausedCount}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                        paused
                    </Typography>
                </Box>
            </Stack>

            {jobs.length === 0 ? (
                <Typography color="text.secondary">
                    No scheduled jobs registered. Apply Alembic migrations to
                    seed the jobstore.
                </Typography>
            ) : (
                jobs.map((job) => (
                    <JobRow key={job.id} job={job} onAfterMutation={refetch} />
                ))
            )}
        </Box>
    );
};

const AdminPanel: React.FC = () => {
    const environment = useRelayEnvironment();
    const [refetchKey, setRefetchKey] = useState(0);

    const refetch = () => {
        // Invalidate the cache for the next render and bump fetchKey.
        fetchQuery(environment, AdminQueryNode, {}).subscribe({
            complete: () => setRefetchKey((k) => k + 1),
            error: () => setRefetchKey((k) => k + 1),
        });
    };

    return (
        <React.Suspense fallback={<Loader open={true} />}>
            <AdminPanelInner refetchKey={refetchKey} refetch={refetch} />
        </React.Suspense>
    );
};

export default AdminPanel;
