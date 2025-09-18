import React, { useState, useRef } from "react";
import { Chip, Snackbar, Alert } from "@mui/material";
import AttributeChipPopper from "./AttributeChipPopper";
import { OPERATORS } from "./constants";

// Custom color palette for chips (expandable)
const CHIP_COLORS = [
    "#1976d2", // blue
    "#9c27b0", // purple
    "#388e3c", // green
    "#d32f2f", // red
    "#0288d1", // light blue
    "#fbc02d", // yellow
    "#f57c00", // orange
    "#455a64", // blue grey
    "#7b1fa2", // deep purple
    "#c2185b", // pink
    "#00796b", // teal
    "#afb42b", // lime
    "#5d4037", // brown
    "#1976d2", // blue (repeat for more keys)
];

// Map specific attribute keys to specific colors
const ATTRIBUTE_KEY_COLORS: Record<string, string> = {
    originalLanguage: "#1976d2", // blue
    status: "#f57c00", // orange
    genres: "#9c27b0", // purple
    network: "#00796b", // teal
};

function hashStringToColorIndex(str: string, paletteSize: number): number {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        hash = str.charCodeAt(i) + ((hash << 5) - hash);
        hash = hash & hash; // Convert to 32bit integer
    }
    return Math.abs(hash) % paletteSize;
}

function getContrastYIQ(hexcolor: string) {
    hexcolor = hexcolor.replace("#", "");
    const r = parseInt(hexcolor.substr(0, 2), 16);
    const g = parseInt(hexcolor.substr(2, 2), 16);
    const b = parseInt(hexcolor.substr(4, 2), 16);
    const yiq = (r * 299 + g * 587 + b * 114) / 1000;
    return yiq >= 128 ? "#222" : "#fff";
}

interface AttributeChipProps {
    name: string;
    value: string;
    itemType: string;
    details: Record<string, unknown> | null;
}

const AttributeChip: React.FC<AttributeChipProps> = ({ name, value, itemType, details }) => {
    const [open, setOpen] = useState(false);
    const [snackbar, setSnackbar] = useState<{ open: boolean; message: string; severity: "success" | "error" }>({ open: false, message: "", severity: "success" });
    const chipRef = useRef<HTMLDivElement | null>(null);
    const [operator, setOperator] = useState<string>(OPERATORS[0].value);

    const handleChipClick = () => setOpen((prev) => !prev);
    const handleClose = () => setOpen(false);

    // Remove handleIgnore and mutation logic

    // Assign a custom background color based on the attribute key
    const bgColor = ATTRIBUTE_KEY_COLORS[name] || CHIP_COLORS[hashStringToColorIndex(name, CHIP_COLORS.length)];
    const textColor = getContrastYIQ(bgColor);

    return (
        <>
            <Chip
                ref={chipRef}
                label={`${name}: ${value}`}
                sx={{
                    marginRight: 0.5,
                    marginBottom: 0.5,
                    backgroundColor: bgColor,
                    color: textColor,
                }}
                onClick={handleChipClick}
            />
            <AttributeChipPopper
                open={open}
                anchorEl={chipRef.current}
                onClose={handleClose}
                operator={operator}
                setOperator={setOperator}
                name={name}
                value={value}
                itemType={itemType}
                details={details}
            />
            <Snackbar
                open={snackbar.open}
                autoHideDuration={3000}
                onClose={() => setSnackbar({ ...snackbar, open: false })}
                anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
            >
                <Alert onClose={() => setSnackbar({ ...snackbar, open: false })} severity={snackbar.severity} sx={{ width: '100%' }}>
                    {snackbar.message}
                </Alert>
            </Snackbar>
        </>
    );
};

export default AttributeChip; 