import React, { useState, useContext, useEffect, useMemo } from "react";
import { TempFilterContext } from "./TempFilterContext";
import {
    Box,
    TextField,
    Select,
    MenuItem,
    Button,
    List,
    ListItem,
    ListItemText,
    IconButton,
    Typography,
} from "@mui/material";
import DeleteIcon from "@mui/icons-material/Delete";
import { graphql, useMutation } from "react-relay";

const operators = [
    "eq",
    "neq",
    "lt",
    "lte",
    "gt",
    "gte",
    "contains",
    "not_contains",
    "in",
    "notin",
];

const RecheckVisibleMutation = graphql`
    mutation TemporaryFilterBarRecheckVisibleMutation($itemType: String!) {
        recheckVisible(itemType: $itemType) {
            id
            attributes {
                key
                values
                details
            }
        }
    }
`;

const TemporaryFilterBar: React.FC<{ selectedComponent: string }> = ({
    selectedComponent,
}) => {
    const { tempFilters, setTempFilters, attributeKeys } =
        useContext(TempFilterContext);
    const [attribute, setAttribute] = useState("");
    const [operator, setOperator] = useState(operators[0]);
    const [value, setValue] = useState("");
    const [recheckVisible, isRechecking] = useMutation(RecheckVisibleMutation);

    useEffect(() => {
        if (attributeKeys.length > 0 && !attribute) {
            setAttribute(attributeKeys[0]);
        }
    }, [attributeKeys, attribute]);

    const addFilter = () => {
        if (!attribute || !operator || !value) return;
        setTempFilters([...tempFilters, { attribute, operator, value }]);
        setAttribute(attributeKeys[0] || "");
        setOperator(operators[0]);
        setValue("");
    };

    const removeFilter = (idx: number) => {
        setTempFilters(tempFilters.filter((_, i) => i !== idx));
    };

    const canRecheck = useMemo(
        () => ["Movies", "TV"].includes(selectedComponent),
        [selectedComponent]
    );
    const handleRecheck = () => {
        if (!canRecheck) return;
        const itemType = selectedComponent === "Movies" ? "mv" : "tv";
        recheckVisible({ variables: { itemType } });
    };

    return (
        <Box mb={2}>
            <Typography variant="h6" gutterBottom>
                Temporary Filters
            </Typography>
            <Box
                display="flex"
                gap={2}
                mb={2}
                alignItems="center"
                flexWrap="wrap"
            >
                {attributeKeys.length > 0 ? (
                    <Select
                        value={attribute}
                        onChange={(e) => setAttribute(e.target.value)}
                        size="small"
                    >
                        {attributeKeys.map((key) => (
                            <MenuItem key={key} value={key}>
                                {key}
                            </MenuItem>
                        ))}
                    </Select>
                ) : (
                    <TextField
                        label="Attribute"
                        value={attribute}
                        onChange={(e) => setAttribute(e.target.value)}
                        size="small"
                    />
                )}
                <Select
                    value={operator}
                    onChange={(e) => setOperator(e.target.value)}
                    size="small"
                >
                    {operators.map((op) => (
                        <MenuItem key={op} value={op}>
                            {op}
                        </MenuItem>
                    ))}
                </Select>
                <TextField
                    label="Value"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    size="small"
                />
                <Button
                    variant="contained"
                    color="primary"
                    onClick={addFilter}
                    sx={{ minWidth: 100 }}
                >
                    Add
                </Button>
                {canRecheck && (
                    <Button
                        variant="outlined"
                        onClick={handleRecheck}
                        disabled={isRechecking}
                    >
                        Recheck
                    </Button>
                )}
            </Box>
            <List dense>
                {tempFilters.map((f, idx) => (
                    <ListItem
                        key={idx}
                        secondaryAction={
                            <IconButton
                                edge="end"
                                aria-label="delete"
                                onClick={() => removeFilter(idx)}
                            >
                                <DeleteIcon />
                            </IconButton>
                        }
                    >
                        <ListItemText
                            primary={`${f.attribute} ${f.operator} ${f.value}`}
                        />
                    </ListItem>
                ))}
            </List>
        </Box>
    );
};
export default TemporaryFilterBar;
